"""처리 완료 아이템 캐시와 중복 판정.

같은 뉴스가 반복해서 오는 경로는 세 가지고, 각각 다른 방법으로 막는다.

1. 같은 피드의 같은 글이 다시 올라온다      → 소스가 준 ID로 판정
2. 같은 기사를 여러 소스가 나른다            → 기사 URL을 정규화해 판정
   (HN 항목의 ID는 news.ycombinator.com 링크지만 실제 기사 링크는 따로다.
    TechCrunch의 ID는 `?p=3153830` 꼴이라 기사 URL과 또 다르다.)
3. 같은 사건을 여러 매체가 각자 쓴다         → 제목 유사도로 판정

판정은 LLM 호출 전에 이뤄지므로 토큰도 함께 아낀다.

Phase 1에서는 Git 저장소 안의 JSON 파일 하나가 곧 상태 저장소다.
`ProcessedStore` 인터페이스만 유지하면 Phase 2에서 Redis/DB 구현으로
갈아끼울 수 있다.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .collectors.base import RawItem

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# 링크에 붙는 추적 파라미터. 값이 달라도 같은 기사다.
_TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid", "igshid", "mc_cid", "mc_eid"}
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_url(url: str) -> str:
    """같은 기사를 가리키는 링크들이 한 값으로 모이도록 정규화한다."""
    if not url:
        return ""

    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"

    kept = sorted(
        f"{key}={value}"
        for key, value in parse_qsl(parts.query)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    )
    query = "?" + "&".join(kept) if kept else ""
    return f"{host}{path}{query}"


def normalize_title(title: str) -> str:
    """대소문자·문장부호·공백 차이를 지운 비교용 제목."""
    folded = unicodedata.normalize("NFKC", title or "").lower()
    return " ".join(_PUNCT_RE.sub(" ", folded).split())


def _similarity(left: str, right: str) -> float:
    # 길이가 크게 다르면 볼 것도 없다. 비싼 비교를 건너뛴다.
    shorter, longer = sorted((len(left), len(right)))
    if not longer or shorter / longer < 0.6:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


class ProcessedStore:
    """이미 처리한 아이템을 기억해 중복 요약·발송을 막는다."""

    def __init__(
        self,
        path: str | Path,
        max_entries: int = 5000,
        similarity_threshold: float = 0.85,
        similarity_window: int = 400,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self.similarity_threshold = similarity_threshold
        self.similarity_window = similarity_window
        self._entries: dict[str, dict[str, Any]] = {}
        self._urls: dict[str, str] = {}
        self._titles: list[tuple[str, str]] = []
        self._dirty = False

    # ------------------------------------------------------------------ 적재

    def load(self) -> "ProcessedStore":
        if not self.path.exists():
            logger.info("캐시 파일이 없어 새로 시작합니다: %s", self.path)
            return self

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 캐시가 깨졌다고 파이프라인을 멈추지는 않는다. 최악의 경우
            # 이번 실행에서 중복 알림이 한 번 나갈 뿐이다.
            logger.warning("캐시 파일을 읽지 못해 빈 캐시로 진행합니다 (%s)", exc)
            return self

        raw_ids = payload.get("ids") if isinstance(payload, dict) else None
        for key, value in (raw_ids or {}).items():
            # v1은 값이 타임스탬프 문자열이었다. 그대로 읽어들인다.
            entry = {"at": value} if isinstance(value, str) else dict(value)
            self._entries[str(key)] = entry
            self._index(str(key), entry)

        logger.info("캐시 %d건 로드: %s", len(self._entries), self.path)
        return self

    def _index(self, key: str, entry: dict[str, Any]) -> None:
        url_key = entry.get("url")
        if url_key:
            self._urls[url_key] = key
        title_key = entry.get("title")
        if title_key:
            self._titles.append((title_key, key))

    # ------------------------------------------------------------------ 조회

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def find_duplicate(self, raw: RawItem) -> str | None:
        """중복이면 사유를, 아니면 None을 돌려준다."""
        if raw.dedup_key in self._entries:
            return "동일 ID"

        url_key = normalize_url(raw.url)
        if url_key and url_key in self._urls:
            return f"동일 URL ({url_key[:60]})"

        if self.similarity_threshold > 0:
            match = self._find_similar_title(normalize_title(raw.title))
            if match:
                return f"유사 제목 ({match[:60]})"
        return None

    def _find_similar_title(self, title_key: str) -> str | None:
        if not title_key:
            return None
        # 최근 것부터 훑는다. 같은 사건은 대개 시간적으로 가깝다.
        for known, _ in reversed(self._titles[-self.similarity_window :]):
            if _similarity(title_key, known) >= self.similarity_threshold:
                return known
        return None

    # ------------------------------------------------------------------ 기록

    def stage(self, raw: RawItem) -> None:
        """저장하지 않고 조회 색인에만 넣는다.

        한 번의 실행 안에서 뒤따르는 아이템이 앞선 아이템과 중복인지 볼 수
        있어야 하는데, 아직 요약에 성공할지는 모르기 때문이다.
        """
        self._index(raw.dedup_key, self._entry_for(raw, at=None))

    def add(self, raw: RawItem, when: datetime | None = None) -> None:
        entry = self._entry_for(raw, at=(when or datetime.now(timezone.utc)).isoformat())
        self._entries[raw.dedup_key] = entry
        self._index(raw.dedup_key, entry)
        self._dirty = True

    @staticmethod
    def _entry_for(raw: RawItem, at: str | None) -> dict[str, Any]:
        entry: dict[str, Any] = {}
        if at:
            entry["at"] = at
        url_key = normalize_url(raw.url)
        if url_key:
            entry["url"] = url_key
        title_key = normalize_title(raw.title)
        if title_key:
            entry["title"] = title_key
        return entry

    def save(self) -> bool:
        """변경이 있을 때만 파일에 쓴다. 기록했으면 True."""
        if not self._dirty:
            logger.info("캐시 변경 없음, 저장 생략")
            return False

        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "ids": self._entries,
        }
        # 원자적 교체: 실행 중 중단돼도 캐시 파일이 반쯤 쓰인 상태로 남지 않는다.
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
        self._dirty = False
        logger.info("캐시 %d건 저장: %s", len(self._entries), self.path)
        return True

    def _prune(self) -> None:
        overflow = len(self._entries) - self.max_entries
        if overflow <= 0:
            return
        # 타임스탬프(ISO8601)는 문자열 정렬이 곧 시간 정렬이다.
        oldest = sorted(self._entries.items(), key=lambda kv: kv[1].get("at", ""))
        for key, _ in oldest[:overflow]:
            del self._entries[key]
        logger.info("캐시 상한(%d) 초과분 %d건 삭제", self.max_entries, overflow)
