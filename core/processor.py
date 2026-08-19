"""LLM 구조화 요약 모듈.

`RawItem` 하나를 받아 Pydantic 스키마(`Insight`)로 강제 파싱된 결과를 돌려준다.

provider는 OpenAI 호환 엔드포인트라면 무엇이든 쓸 수 있고, 설정에 적힌 순서대로
시도한다(기본: Gemini → Groq). 앞 provider가 쿼터 초과나 장애로 실패하면 다음
provider가 같은 아이템을 이어받는다.

스키마 설계 메모: `minimum`/`maxItems` 같은 제약 키워드는 strict 스키마에서
지원 여부가 provider마다 갈린다. 그래서 LLM에 노출되는 스키마는 `Literal`과
평범한 배열만 쓰고, 개수 보정은 파싱 후 파이썬에서 처리한다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

from openai import BadRequestError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .collectors.base import RawItem
from .config import ProviderConfig

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# 429 응답이 알려주는 대기 시간. Gemini는 두 형태를 모두 담아 보낸다.
_RETRY_IN_RE = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s")


def _retry_delay(exc: Exception, fallback: float, cap: float) -> float:
    """429 응답에서 정확한 대기 시간을 뽑아낸다.

    고정 백오프(1·2·4초)로는 분당 요청 제한을 넘길 수 없다. 제한이 풀리는
    시각을 서버가 알려주므로 그 값을 그대로 쓴다. 다만 통째로 믿으면 job이
    타임아웃될 수 있어 상한을 둔다.
    """
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("retry-after") if response else None
    if header:
        try:
            return min(float(header), cap)
        except ValueError:
            pass

    text = str(exc)
    match = _RETRY_IN_RE.search(text) or _RETRY_DELAY_RE.search(text)
    if match:
        # 서버가 알려준 시각 직후에 다시 때리면 아슬아슬하게 또 막힌다.
        return min(float(match.group(1)) + 1.0, cap)
    return fallback


class Category(str, Enum):
    """도메인 분류. 도메인을 바꿀 때 가장 먼저 손대는 지점."""

    LLM = "LLM"
    VISION = "Vision"
    INFRA = "Infra"
    RESEARCH = "Research"
    INDUSTRY = "Industry"
    PRODUCT = "Product"
    POLICY = "Policy"
    OTHER = "Other"


class Insight(BaseModel):
    """LLM이 채워야 하는 정형 출력."""

    model_config = ConfigDict(extra="forbid")

    title_ko: str = Field(description="한국어 제목. 25자 내외의 명사형.")
    summary_ko: list[str] = Field(
        description="핵심 요약 3줄. 각 줄은 60자 내외의 완결된 한국어 문장."
    )
    category: Category = Field(description="아래 분류 중 가장 알맞은 하나.")
    importance: Literal[1, 2, 3, 4, 5] = Field(
        description=(
            "AI 실무자 기준 중요도. "
            "1=잡담/홍보, 2=참고, 3=알아둘 만함, 4=중요, 5=반드시 확인."
        )
    )


@dataclass(slots=True)
class ProcessedItem:
    """원본 + 요약 결과 묶음. 알림/저장 단계가 소비한다."""

    raw: RawItem
    insight: Insight
    provider: str = ""
    """어느 provider가 요약했는지 (폴백 발생 여부 추적용)."""

    @property
    def importance(self) -> int:
        return int(self.insight.importance)


SYSTEM_PROMPT = """당신은 AI 분야 기술 뉴스를 선별하는 시니어 리서처입니다.
입력으로 받은 글 하나를 한국어로 요약하고 분류·채점하세요.

규칙:
- 한국어로 작성하되, 고유명사와 기술 용어(예: GPT-4o, RAG, CUDA)는 원문 표기를 유지합니다.
- summary_ko는 정확히 3개 문장으로, 무엇이/왜 중요한지가 드러나게 씁니다.
- 원문에 없는 사실을 추측하거나 지어내지 않습니다. 정보가 부족하면 부족한 대로 요약합니다.
- importance는 화제성이 아니라 실무 영향도를 기준으로 매깁니다.
  단순 홍보, 개인 잡담, 중복 보도는 1~2점을 줍니다."""

# json_object 모드(strict 스키마 미지원 provider)에서만 덧붙인다.
JSON_MODE_SUFFIX = """

반드시 아래 JSON 스키마를 그대로 따르는 JSON 객체 하나만 출력하세요.
설명, 주석, 마크다운 코드펜스 없이 JSON 본문만 출력합니다.

{schema}"""

USER_TEMPLATE = """[출처] {source} ({source_type})
[제목] {title}
[작성자] {author}
[작성일] {published_at}
[URL] {url}

[본문]
{content}"""


@dataclass
class _Provider:
    """설정 + 연결된 클라이언트 + 런타임에 조정되는 출력 모드."""

    config: ProviderConfig
    client: OpenAI
    parse: Callable[..., Any]
    mode: str

    @property
    def name(self) -> str:
        return self.config.name


class Processor:
    """OpenAI 호환 provider 체인 기반 요약기."""

    def __init__(
        self,
        providers: list[ProviderConfig],
        temperature: float = 0.2,
        max_retries: int = 3,
        max_content_chars: int = 4000,
        max_retry_delay: float = 60.0,
    ) -> None:
        self.temperature = temperature
        self.max_retries = max_retries
        self.max_content_chars = max_content_chars
        self.max_retry_delay = max_retry_delay
        self._providers = self._build_providers(providers)

        if not self._providers:
            wanted = ", ".join(p.api_key_env for p in providers)
            raise RuntimeError(
                f"사용 가능한 LLM provider가 없습니다. 다음 중 하나는 설정해야 합니다: {wanted}"
            )
        logger.info(
            "LLM provider 순서: %s",
            " → ".join(f"{p.name}({p.config.model})" for p in self._providers),
        )

    @staticmethod
    def _build_providers(configs: list[ProviderConfig]) -> list[_Provider]:
        """API 키가 있는 provider만 체인에 넣는다."""
        built: list[_Provider] = []
        for config in configs:
            # 붙여넣기·시크릿 등록 과정에서 끝에 개행이 섞여도 인증이 깨지지 않게 한다.
            api_key = (os.getenv(config.api_key_env) or "").strip()
            if not api_key:
                logger.info(
                    "%s 미설정 → provider '%s' 건너뜀", config.api_key_env, config.name
                )
                continue
            # SDK 내장 재시도는 백오프가 1초 미만이라 분당 제한에 무력하다.
            # 재시도는 아래 `_summarize`가 서버가 알려준 간격으로 직접 한다.
            client = OpenAI(
                api_key=api_key, base_url=config.base_url, max_retries=0
            )
            built.append(
                _Provider(
                    config=config,
                    client=client,
                    parse=Processor._resolve_parse(client),
                    mode=config.structured_output,
                )
            )
        return built

    @staticmethod
    def _resolve_parse(client: OpenAI) -> Callable[..., Any]:
        """SDK 버전에 따라 `chat.completions.parse` 위치가 다르다."""
        parse = getattr(client.chat.completions, "parse", None)
        if parse is not None:
            return parse
        return client.beta.chat.completions.parse

    def process(self, item: RawItem) -> ProcessedItem | None:
        """아이템 하나를 요약한다. 모든 provider가 실패하면 None."""
        for index, provider in enumerate(self._providers):
            insight = self._summarize(provider, item)
            if insight is not None:
                insight.summary_ko = _normalize_summary(insight.summary_ko)
                return ProcessedItem(raw=item, insight=insight, provider=provider.name)

            remaining = len(self._providers) - index - 1
            if remaining:
                logger.warning(
                    "provider '%s' 실패 → 다음 provider로 폴백합니다", provider.name
                )
        logger.error("모든 provider 실패: %s", item.title[:60])
        return None

    def process_many(self, items: list[RawItem]) -> list[ProcessedItem]:
        results: list[ProcessedItem] = []
        for index, item in enumerate(items, start=1):
            logger.info("요약 %d/%d: %s", index, len(items), item.title[:70])
            processed = self.process(item)
            if processed is not None:
                results.append(processed)
        return results

    def _summarize(self, provider: _Provider, item: RawItem) -> Insight | None:
        """provider 하나로 재시도까지 수행한다. 최종 실패면 None."""
        for attempt in range(1, self.max_retries + 1):
            delay = float(2 ** (attempt - 1))
            try:
                return self._call(provider, item)
            except RateLimitError as exc:
                delay = _retry_delay(exc, fallback=delay, cap=self.max_retry_delay)
                logger.warning(
                    "[%s] 레이트 리밋 (%d/%d), %.1f초 대기",
                    provider.name, attempt, self.max_retries, delay,
                )
            except BadRequestError as exc:
                # strict 스키마를 거부하는 provider가 있다. 한 단계 낮춰 다시 시도한다.
                if provider.mode == "json_schema":
                    logger.warning(
                        "provider '%s'가 json_schema를 거부했습니다. json_object 모드로 전환합니다 (%s)",
                        provider.name,
                        exc,
                    )
                    provider.mode = "json_object"
                    continue
                logger.error("[%s] 잘못된 요청: %s", provider.name, exc)
                return None
            except (ValidationError, json.JSONDecodeError) as exc:
                logger.warning("[%s] 응답 파싱 실패 (%d/%d): %s", provider.name, attempt, self.max_retries, exc)
            except Exception as exc:  # noqa: BLE001 - 쿼터/네트워크 장애는 재시도 후 폴백
                logger.warning("[%s] 호출 실패 (%d/%d): %s", provider.name, attempt, self.max_retries, exc)

            if attempt < self.max_retries:
                time.sleep(delay)
        return None

    def _call(self, provider: _Provider, item: RawItem) -> Insight | None:
        json_mode = provider.mode == "json_object"
        messages = [
            {"role": "system", "content": self._system_prompt(json_mode)},
            {"role": "user", "content": self._render_user_prompt(item)},
        ]
        kwargs: dict[str, Any] = {
            "model": provider.config.model,
            "messages": messages,
            "temperature": self.temperature,
        }

        if json_mode:
            completion = provider.client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs
            )
            message = completion.choices[0].message
            if getattr(message, "refusal", None):
                logger.warning("모델이 요약을 거부했습니다: %s", item.title[:60])
                return None
            return Insight.model_validate_json(_strip_fence(message.content or ""))

        completion = provider.parse(response_format=Insight, **kwargs)
        message = completion.choices[0].message
        if getattr(message, "refusal", None):
            logger.warning("모델이 요약을 거부했습니다: %s", item.title[:60])
            return None
        if message.parsed is None:
            raise ValueError("파싱 결과가 비어 있습니다")
        return message.parsed

    @staticmethod
    def _system_prompt(json_mode: bool) -> str:
        if not json_mode:
            return SYSTEM_PROMPT
        schema = json.dumps(Insight.model_json_schema(), ensure_ascii=False, indent=2)
        return SYSTEM_PROMPT + JSON_MODE_SUFFIX.format(schema=schema)

    def _render_user_prompt(self, item: RawItem) -> str:
        return USER_TEMPLATE.format(
            source=item.source,
            source_type=item.source_type,
            title=item.title,
            author=item.author or "(미상)",
            published_at=item.published_at.isoformat() if item.published_at else "(미상)",
            url=item.url,
            content=(item.content or "(본문 없음. 제목과 출처만으로 판단하세요.)")[
                : self.max_content_chars
            ],
        )


def _strip_fence(text: str) -> str:
    """```json ... ``` 로 감싸 보내는 모델 대응."""
    return _FENCE_RE.sub("", text.strip())


def _normalize_summary(lines: list[str]) -> list[str]:
    """요약을 정확히 3줄로 맞춘다 (모자라면 있는 만큼만)."""
    cleaned = [line.strip().lstrip("-•· ").strip() for line in lines]
    return [line for line in cleaned if line][:3]
