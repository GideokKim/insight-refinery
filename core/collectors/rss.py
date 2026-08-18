"""RSS / Atom 피드 수집기."""

from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import feedparser

from .base import Collector, RawItem, iter_limited

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(value: str) -> str:
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", value or "")).strip()


def _to_datetime(struct_time: time.struct_time | None) -> datetime | None:
    """feedparser의 `*_parsed`는 UTC 기준이다.

    `time.mktime()`은 인자를 로컬 시각으로 해석하므로 여기서 쓰면 로컬
    오프셋만큼 시각이 밀린다. UTC로 해석하는 `calendar.timegm()`을 쓴다.
    """
    if not struct_time:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(struct_time), tz=timezone.utc)
    except (OverflowError, ValueError):
        return None


class RSSCollector(Collector):
    """`feedparser`로 RSS/Atom 피드를 읽어 `RawItem`으로 변환한다.

    options:
        url (str, 필수): 피드 주소
        limit (int): 한 번에 가져올 최대 엔트리 수 (기본 20)
        max_content_chars (int): 본문 길이 상한 (기본 4000)
    """

    type = "rss"

    def collect(self) -> Iterable[RawItem]:
        url = self._option("url")
        if not url:
            raise ValueError(f"[{self.name}] rss 소스에는 'url' 옵션이 필요합니다")

        limit = self._option("limit", 20)
        max_chars = self._option("max_content_chars", 4000)

        feed = feedparser.parse(url)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(f"피드 파싱 실패: {feed.get('bozo_exception', 'unknown')}")

        for entry in iter_limited(feed.entries, limit):
            item = self._to_item(entry, max_chars)
            if item is not None:
                yield item

    def _to_item(self, entry: Any, max_chars: int) -> RawItem | None:
        link = entry.get("link") or ""
        external_id = entry.get("id") or link
        title = _strip_html(entry.get("title") or "")
        if not external_id or not title:
            logger.debug("[%s] id/title 없는 엔트리 건너뜀", self.name)
            return None

        published = _to_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )

        return RawItem(
            source=self.name,
            source_type=self.type,
            external_id=external_id,
            title=title,
            url=link,
            content=self._extract_content(entry)[:max_chars],
            author=entry.get("author") or None,
            published_at=published,
            extra={"tags": [t.get("term") for t in entry.get("tags", []) if t.get("term")]},
        )

    @staticmethod
    def _extract_content(entry: Any) -> str:
        contents = entry.get("content") or []
        if contents:
            return _strip_html(contents[0].get("value", ""))
        return _strip_html(entry.get("summary") or entry.get("description") or "")
