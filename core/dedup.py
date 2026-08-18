"""처리 완료 아이템 캐시.

Phase 1에서는 Git 저장소 안의 JSON 파일 하나가 곧 상태 저장소다.
`ProcessedStore` 인터페이스만 유지하면 Phase 2에서 Redis/DB 구현으로
갈아끼울 수 있다.

저장 형식:
    {
      "version": 1,
      "updated_at": "2026-08-18T09:00:00+00:00",
      "ids": {"rss:https://example.com/a": "2026-08-18T09:00:00+00:00"}
    }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class ProcessedStore:
    """이미 처리한 아이템 키를 기억해 LLM 재호출을 막는다."""

    def __init__(self, path: str | Path, max_entries: int = 5000) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self._ids: dict[str, str] = {}
        self._dirty = False

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

        ids = payload.get("ids") if isinstance(payload, dict) else None
        if isinstance(ids, dict):
            self._ids = {str(k): str(v) for k, v in ids.items()}
        logger.info("캐시 %d건 로드: %s", len(self._ids), self.path)
        return self

    def __contains__(self, key: str) -> bool:
        return key in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, key: str, when: datetime | None = None) -> None:
        timestamp = (when or datetime.now(timezone.utc)).isoformat()
        if self._ids.get(key) != timestamp:
            self._ids[key] = timestamp
            self._dirty = True

    def add_many(self, keys: Iterable[str]) -> None:
        now = datetime.now(timezone.utc)
        for key in keys:
            self.add(key, now)

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
            "ids": self._ids,
        }
        # 원자적 교체: 실행 중 중단돼도 캐시 파일이 반쯤 쓰인 상태로 남지 않는다.
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)
        self._dirty = False
        logger.info("캐시 %d건 저장: %s", len(self._ids), self.path)
        return True

    def _prune(self) -> None:
        overflow = len(self._ids) - self.max_entries
        if overflow <= 0:
            return
        # 타임스탬프(ISO8601)는 문자열 정렬이 곧 시간 정렬이다.
        oldest = sorted(self._ids.items(), key=lambda kv: kv[1])[:overflow]
        for key, _ in oldest:
            del self._ids[key]
        logger.info("캐시 상한(%d) 초과분 %d건 삭제", self.max_entries, overflow)
