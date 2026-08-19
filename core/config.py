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

DEFAULT_PROVIDERS: list[dict[str, str]] = [
    {
        "name": "gemini",
        "model": "gemini-3.5-flash-lite",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    {
        "name": "groq",
        "model": "openai/gpt-oss-20b",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
]


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


class ProviderConfig(BaseModel):
    """LLM provider 하나. 전부 OpenAI 호환 엔드포인트를 전제로 한다."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    api_key_env: str
    base_url: str | None = None
    """None이면 OpenAI 기본 엔드포인트를 쓴다."""

    structured_output: Literal["json_schema", "json_object"] = "json_schema"
    """provider가 strict 스키마를 거부하면 런타임에 json_object로 낮춘다."""


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: list[ProviderConfig] = Field(
        default_factory=lambda: [ProviderConfig(**p) for p in DEFAULT_PROVIDERS],
        min_length=1,
    )
    """앞에서부터 시도하고, 실패하면 다음 provider로 넘어간다."""

    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_retries: int = Field(default=3, ge=1)
    max_content_chars: int = Field(default=4000, ge=200)
    max_retry_delay: float = Field(default=60.0, ge=0.0)


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("data/processed_ids.json")
    max_entries: int = Field(default=5000, ge=1)
    """오래된 키부터 잘라내 캐시 파일이 무한정 커지는 것을 막는다."""


class EmailConfig(BaseModel):
    """SMTP 다이제스트 설정. 계정/비밀번호는 환경 변수로만 받는다."""

    model_config = ConfigDict(extra="forbid")

    host: str = "smtp.gmail.com"
    port: int = Field(default=587, ge=1, le=65535)
    use_tls: bool = True
    """587에서 STARTTLS 승격 여부. 465는 포트만 보고 SMTP_SSL을 쓴다."""

    sender: str | None = None
    """None이면 SMTP_USER를 발신자로 쓴다."""

    recipients: list[str] = Field(default_factory=list)
    """비우면 SMTP_USER 본인에게 보낸다."""

    subject_prefix: str = "[insight-refinery]"

    digest_hour: int | None = Field(default=None, ge=0, le=23)
    """이 UTC 시각의 실행에서만 발송한다. None이면 매 실행 발송.

    다른 실행에서는 큐에 쌓아두기만 하므로, 값은 cron이 실제로 도는 시각 중
    하나여야 한다. 아니면 영영 발송되지 않는다.
    """

    queue_path: Path = Path("data/digest_queue.json")
    max_queue_entries: int = Field(default=500, ge=1)


class NotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channels: list[Literal["telegram", "discord", "email"]] = Field(
        default_factory=lambda: ["discord"]
    )
    """모두에게 보낸다. 자격 증명이 없는 채널은 건너뛴다."""

    min_importance: dict[str, int] = Field(default_factory=dict)
    """채널별 임계치 override. 없는 채널은 `run.min_importance`를 쓴다."""

    rate_limit_delay: float = Field(default=0.5, ge=0.0)
    disable_web_page_preview: bool = False
    email: EmailConfig = Field(default_factory=EmailConfig)

    def threshold_for(self, channel: str, default: int) -> int:
        return self.min_importance.get(channel, default)


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
