"""`config.yaml` 로딩과 검증.

설정 스키마 자체를 Pydantic으로 정의해, 잘못된 설정이 파이프라인 중간이
아니라 시작 시점에 드러나게 한다. 비밀 값(API 키/토큰)은 설정 파일이 아니라
환경 변수로만 주입한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_PATH = Path("config.yaml")


class SourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_items_per_run: int = Field(default=25, ge=1)
    """한 번 실행에서 LLM에 넘길 최대 아이템 수 (토큰 비용 상한)."""

    min_importance: int = Field(default=3, ge=1, le=5)
    """이 점수 이상만 알림으로 발송한다."""


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_retries: int = Field(default=3, ge=1)
    max_content_chars: int = Field(default=4000, ge=200)


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("data/processed_ids.json")
    max_entries: int = Field(default=5000, ge=1)
    """오래된 키부터 잘라내 캐시 파일이 무한정 커지는 것을 막는다."""


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: Literal["telegram"] = "telegram"
    rate_limit_delay: float = Field(default=0.5, ge=0.0)
    disable_web_page_preview: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: RunConfig = Field(default_factory=RunConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    sources: list[SourceConfig] = Field(default_factory=list)

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [source for source in self.sources if source.enabled]


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """YAML 설정을 읽어 `Config`로 검증한다."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"설정 파일의 최상위는 매핑이어야 합니다: {config_path}")

    return Config.model_validate(raw)
