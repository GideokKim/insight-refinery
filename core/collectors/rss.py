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

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after(headers: dict, fallback: float) -> float:
    """서버가 Retry-After를 주면 그 값을, 아니면 기본 대기를 쓴다."""
    try:
        return max(float(headers.get("retry-after", "")), fallback)
    except (TypeError, ValueError):
        return fallback


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
        user_agent (str): 기본 UA를 차단하는 피드용 User-Agent
        retries (int): 429/5xx 재시도 횟수 (기본 3)
        retry_delay (float): 재시도 기본 대기 초 (기본 5)
    """

    type = "rss"

    def collect(self) -> Iterable[RawItem]:
        url = self._option("url")
        if not url:
            raise ValueError(f"[{self.name}] rss 소스에는 'url' 옵션이 필요합니다")

        limit = self._option("limit", 20)
        max_chars = self._option("max_content_chars", 4000)

        feed = self._fetch(url)
        if getattr(feed, "bozo", False) and not feed.entries:
            raise RuntimeError(f"피드 파싱 실패: {feed.get('bozo_exception', 'unknown')}")

        for entry in iter_limited(feed.entries, limit):
            item = self._to_item(entry, max_chars)
            if item is not None:
                yield item

    def _fetch(self, url: str) -> Any:
        """피드를 가져오되 일시적 거절은 재시도한다.

        Reddit RSS는 연속 요청에 429를 잘 뱉지만 몇 초 뒤엔 통과한다.
        한 번 막혔다고 그 소스를 통째로 버리면 손해다.
        """
        retries = int(self._option("retries", 3))
        base_delay = float(self._option("retry_delay", 5))
        # feedparser 기본 UA는 차단하는 사이트가 있다(Reddit 등).
        agent = self._option("user_agent")

        status = 200
        for attempt in range(1, retries + 1):
            feed = feedparser.parse(url, agent=agent) if agent else feedparser.parse(url)
            status = getattr(feed, "status", 200)
            if status not in _RETRYABLE_STATUS:
                return feed

            if attempt < retries:
                delay = _retry_after(getattr(feed, "headers", {}), base_delay * attempt)
                logger.info(
                    "[%s] HTTP %d, %.1f초 후 재시도 (%d/%d)",
                    self.name, status, delay, attempt, retries,
                )
                time.sleep(delay)

        # 거절 응답도 본문이 비어 있을 뿐 bozo는 False라, 그냥 돌려주면
        # "0건 수집"으로 보여 실패가 성공처럼 묻힌다. 명시적으로 실패시킨다.
        raise RuntimeError(f"HTTP {status}, 재시도 {retries}회 모두 실패")

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
