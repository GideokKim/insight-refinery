"""테스트 공용 팩토리.

네트워크와 시계에 의존하지 않는다. 외부 호출은 전부 대역으로 바꾸고,
시각은 인자로 주입한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.collectors.base import RawItem
from core.processor import Category, Insight, ProcessedItem

UTC = timezone.utc


@pytest.fixture
def raw_item():
    def _make(
        external_id: str = "1",
        title: str = "제목",
        url: str = "https://example.com/a",
        source: str = "테스트 소스",
        source_type: str = "rss",
        published_at: datetime | None = datetime(2026, 8, 19, 9, 0, tzinfo=UTC),
        content: str = "본문",
    ) -> RawItem:
        return RawItem(
            source=source,
            source_type=source_type,
            external_id=external_id,
            title=title,
            url=url,
            content=content,
            published_at=published_at,
        )

    return _make


@pytest.fixture
def insight():
    def _make(
        importance: int = 4,
        title_ko: str = "한국어 제목",
        summary_ko: list[str] | None = None,
        category: Category = Category.LLM,
    ) -> Insight:
        return Insight(
            title_ko=title_ko,
            summary_ko=summary_ko or ["첫째 줄", "둘째 줄", "셋째 줄"],
            category=category,
            importance=importance,
        )

    return _make


@pytest.fixture
def processed(raw_item, insight):
    def _make(index: int = 1, importance: int = 4, **kwargs) -> ProcessedItem:
        return ProcessedItem(
            raw=raw_item(
                external_id=f"id{index}",
                title=f"제목 {index}",
                url=f"https://example.com/{index}",
                **kwargs,
            ),
            insight=insight(importance=importance),
            provider="gemini",
        )

    return _make
