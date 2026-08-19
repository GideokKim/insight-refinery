"""다이제스트 대기열.

실시간 채널(Discord)은 실행할 때마다 바로 보내지만, 이메일은 하루 한 번만
보낸다. 그런데 배치는 3시간마다 돌고 처리한 아이템은 중복 방지 캐시에 기록돼
다시 요약되지 않으므로, 발송 시각까지 요약 결과 자체를 들고 있어야 한다.
그 보관소가 이 큐다.

`data/digest_queue.json`에 쌓이고, 발송에 성공한 뒤에만 비워진다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .collectors.base import RawItem
from .processor import Insight, ProcessedItem

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def to_dict(item: ProcessedItem) -> dict[str, Any]:
    raw = item.raw
    return {
        "raw": {
            "source": raw.source,
            "source_type": raw.source_type,
            "external_id": raw.external_id,
            "title": raw.title,
            "url": raw.url,
            "author": raw.author,
            "published_at": raw.published_at.isoformat() if raw.published_at else None,
        },
        "insight": item.insight.model_dump(mode="json"),
        "provider": item.provider,
    }


def from_dict(payload: dict[str, Any]) -> ProcessedItem:
    raw = dict(payload["raw"])
    published = raw.pop("published_at", None)
    return ProcessedItem(
        raw=RawItem(
            **raw,
            published_at=datetime.fromisoformat(published) if published else None,
        ),
        insight=Insight.model_validate(payload["insight"]),
        provider=payload.get("provider", ""),
    )


class DigestQueue:
    """발송 대기 중인 요약을 보관한다."""

    def __init__(self, path: str | Path, max_entries: int = 500) -> None:
        self.path = Path(path)
        self.max_entries = max_entries
        self._items: list[ProcessedItem] = []
        self._dirty = False

    def load(self) -> "DigestQueue":
        if not self.path.exists():
            return self

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [from_dict(entry) for entry in payload.get("items", [])]
        except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
            # 큐가 깨져도 이번 실행을 막지는 않는다. 최악의 경우 대기분만 잃는다.
            logger.warning("다이제스트 큐를 읽지 못해 빈 큐로 진행합니다 (%s)", exc)
            self._items = []
        else:
            logger.info("다이제스트 큐 %d건 로드", len(self._items))
        return self

    def __len__(self) -> int:
        return len(self._items)

    def items(self) -> list[ProcessedItem]:
        return list(self._items)

    def extend(self, items: Iterable[ProcessedItem]) -> int:
        """이미 담긴 것과 중복되지 않는 항목만 추가한다."""
        known = {item.raw.dedup_key for item in self._items}
        added = 0
        for item in items:
            if item.raw.dedup_key in known:
                continue
            known.add(item.raw.dedup_key)
            self._items.append(item)
            added += 1

        if added:
            self._dirty = True
            self._prune()
        return added

    def clear(self) -> None:
        if self._items:
            self._items = []
            self._dirty = True

    def save(self) -> bool:
        if not self._dirty:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "items": [to_dict(item) for item in self._items],
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp_path.replace(self.path)
        self._dirty = False
        logger.info("다이제스트 큐 %d건 저장", len(self._items))
        return True

    def _prune(self) -> None:
        overflow = len(self._items) - self.max_entries
        if overflow > 0:
            # 오래된 것부터 버린다. 다이제스트는 최신 소식이 목적이다.
            self._items = self._items[overflow:]
            logger.warning("다이제스트 큐 상한 초과분 %d건 삭제", overflow)


def is_digest_due(now: datetime, digest_hour: int) -> bool:
    """지금이 다이제스트 발송 시각인지. 배치가 시간 단위로 돌기에 시(hour)만 본다."""
    return now.hour == digest_hour
