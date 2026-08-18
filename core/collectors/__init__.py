"""수집기 패키지.

여기서 각 구현 모듈을 import 하는 것만으로 `Collector._registry`에 등록된다.
새 소스를 추가하려면 모듈을 만들고 아래 import 목록에 한 줄 더하면 된다.
"""

from __future__ import annotations

from .base import Collector, RawItem
from .reddit import RedditCollector
from .rss import RSSCollector

__all__ = ["Collector", "RawItem", "RSSCollector", "RedditCollector", "build_collectors"]


def build_collectors(sources: list) -> list[Collector]:
    """`SourceConfig` 목록에서 활성화된 수집기 인스턴스를 만든다."""
    return [
        Collector.create(source.name, source.type, source.options)
        for source in sources
        if source.enabled
    ]
