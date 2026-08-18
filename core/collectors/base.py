"""Collector 공통 인터페이스와 수집 결과 스키마.

모든 수집기는 `Collector`를 상속하고 `type` 클래스 변수를 선언한다.
선언과 동시에 레지스트리에 등록되므로, `config.yaml`의 `sources[].type`
문자열만으로 인스턴스를 만들 수 있다 (`Collector.create`).

이 모듈은 배치/스트리밍 실행 방식에 의존하지 않는다. Phase 2에서
상시 가동 워커로 전환할 때도 그대로 재사용한다.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Iterable, Iterator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RawItem:
    """수집기가 반환하는 정규화된 원본 아이템."""

    source: str
    """`config.yaml`에 적힌 소스 이름 (예: "OpenAI News")."""

    source_type: str
    """수집기 타입 (예: "rss", "reddit")."""

    external_id: str
    """소스 내에서 안정적으로 유일한 식별자. 중복 판정의 기준."""

    title: str
    url: str
    content: str = ""
    author: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """`data/processed_ids.json`에 저장되는 전역 중복 판정 키."""
        return f"{self.source_type}:{self.external_id}"


class Collector(abc.ABC):
    """소스별 수집기의 베이스 클래스."""

    type: ClassVar[str] = ""
    _registry: ClassVar[dict[str, type["Collector"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.type:
            Collector._registry[cls.type] = cls

    def __init__(self, name: str, options: dict[str, Any] | None = None) -> None:
        self.name = name
        self.options = options or {}

    @abc.abstractmethod
    def collect(self) -> Iterable[RawItem]:
        """소스에서 아이템을 가져온다. 실패 시 예외를 던져도 된다."""

    def safe_collect(self) -> list[RawItem]:
        """수집 실패가 파이프라인 전체를 중단시키지 않도록 감싼 진입점."""
        try:
            items = list(self.collect())
        except Exception:  # noqa: BLE001 - 소스 하나의 장애는 격리한다
            logger.exception("[%s] 수집 실패 (건너뜀)", self.name)
            return []
        logger.info("[%s] %d건 수집", self.name, len(items))
        return items

    @classmethod
    def create(
        cls, name: str, type_: str, options: dict[str, Any] | None = None
    ) -> "Collector":
        try:
            impl = cls._registry[type_]
        except KeyError:
            known = ", ".join(sorted(cls._registry)) or "(없음)"
            raise ValueError(
                f"알 수 없는 수집기 타입 '{type_}' (사용 가능: {known})"
            ) from None
        return impl(name, options)

    @classmethod
    def registered_types(cls) -> list[str]:
        return sorted(cls._registry)

    def _option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의용
        return f"<{type(self).__name__} name={self.name!r}>"


def iter_limited(items: Iterable[RawItem], limit: int | None) -> Iterator[RawItem]:
    """수집기 구현에서 공통으로 쓰는 상한 적용 헬퍼."""
    for index, item in enumerate(items):
        if limit is not None and index >= limit:
            return
        yield item
