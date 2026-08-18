"""LLM 구조화 요약 모듈.

`RawItem` 하나를 받아 Pydantic 스키마(`Insight`)로 강제 파싱된 결과를 돌려준다.
OpenAI Structured Outputs를 쓰므로 형식이 어긋난 응답은 SDK 단계에서 걸러진다.

스키마 설계 메모: `minimum`/`maxItems` 같은 제약 키워드는 strict 스키마에서
지원 여부가 SDK/모델 버전마다 갈린다. 그래서 LLM에 노출되는 스키마는
`Literal`과 평범한 배열만 쓰고, 개수 보정은 파싱 후 파이썬에서 처리한다.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field

from .collectors.base import RawItem

logger = logging.getLogger(__name__)


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

USER_TEMPLATE = """[출처] {source} ({source_type})
[제목] {title}
[작성자] {author}
[작성일] {published_at}
[URL] {url}

[본문]
{content}"""


class Processor:
    """OpenAI Structured Outputs 기반 요약기."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        temperature: float = 0.2,
        max_retries: int = 3,
        max_content_chars: int = 4000,
        api_key: str | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.max_retries = max_retries
        self.max_content_chars = max_content_chars

        if client is None:
            key = api_key or os.getenv("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY 환경 변수가 필요합니다")
            client = OpenAI(api_key=key)
        self._client = client
        self._parse: Callable[..., Any] = self._resolve_parse(client)

    @staticmethod
    def _resolve_parse(client: OpenAI) -> Callable[..., Any]:
        """SDK 버전에 따라 `chat.completions.parse` 위치가 다르다."""
        parse = getattr(client.chat.completions, "parse", None)
        if parse is not None:
            return parse
        return client.beta.chat.completions.parse

    def process(self, item: RawItem) -> ProcessedItem | None:
        """아이템 하나를 요약한다. 최종 실패 시 None."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._render_user_prompt(item)},
        ]

        for attempt in range(1, self.max_retries + 1):
            try:
                completion = self._parse(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    response_format=Insight,
                )
            except Exception as exc:  # noqa: BLE001 - 재시도 후 스킵
                if attempt == self.max_retries:
                    logger.error("요약 실패 (%s): %s", item.title[:60], exc)
                    return None
                delay = 2 ** (attempt - 1)
                logger.warning("요약 재시도 %d/%d (%ss 후): %s", attempt, self.max_retries, delay, exc)
                time.sleep(delay)
                continue

            message = completion.choices[0].message
            if getattr(message, "refusal", None):
                logger.warning("모델이 요약을 거부했습니다: %s", item.title[:60])
                return None

            insight = message.parsed
            if insight is None:
                logger.warning("파싱 결과가 비어 있습니다: %s", item.title[:60])
                return None

            insight.summary_ko = _normalize_summary(insight.summary_ko)
            return ProcessedItem(raw=item, insight=insight)

        return None

    def process_many(self, items: list[RawItem]) -> list[ProcessedItem]:
        results: list[ProcessedItem] = []
        for index, item in enumerate(items, start=1):
            logger.info("요약 %d/%d: %s", index, len(items), item.title[:70])
            processed = self.process(item)
            if processed is not None:
                results.append(processed)
        return results

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


def _normalize_summary(lines: list[str]) -> list[str]:
    """요약을 정확히 3줄로 맞춘다 (모자라면 있는 만큼만)."""
    cleaned = [line.strip().lstrip("-•· ").strip() for line in lines]
    return [line for line in cleaned if line][:3]
