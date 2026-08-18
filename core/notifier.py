"""알림 전송 모듈.

`Notifier` 인터페이스만 지키면 Slack/Discord 구현을 추가해도
파이프라인 본체는 바뀌지 않는다.
"""

from __future__ import annotations

import abc
import logging
import os
import time
from typing import Iterable

import httpx

from .processor import ProcessedItem

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Telegram MarkdownV2에서 반드시 이스케이프해야 하는 문자들.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_TABLE = str.maketrans({char: "\\" + char for char in _MDV2_SPECIALS})
_URL_TABLE = str.maketrans({")": "\\)", "\\": "\\\\"})


def escape_md(text: str) -> str:
    """MarkdownV2 본문용 이스케이프."""
    return (text or "").translate(_MDV2_TABLE)


def escape_url(url: str) -> str:
    """MarkdownV2 링크 URL용 이스케이프 (본문과 규칙이 다르다)."""
    return (url or "").translate(_URL_TABLE)


def format_message(item: ProcessedItem) -> str:
    """항목 하나를 Telegram MarkdownV2 메시지로 변환한다."""
    insight = item.insight
    raw = item.raw
    stars = "★" * item.importance + "☆" * (5 - item.importance)

    lines = [
        f"*{escape_md(insight.title_ko)}*",
        escape_md(f"{stars} · {insight.category.value} · {raw.source}"),
        "",
    ]
    lines += [f"• {escape_md(sentence)}" for sentence in insight.summary_ko]
    if raw.url:
        lines += ["", f"[원문 보기]({escape_url(raw.url)})"]
    return "\n".join(lines)


class Notifier(abc.ABC):
    """알림 채널 공통 인터페이스."""

    @abc.abstractmethod
    def send(self, item: ProcessedItem) -> bool:
        """한 건 전송. 성공 여부를 돌려준다."""

    def send_many(self, items: Iterable[ProcessedItem]) -> int:
        return sum(1 for item in items if self.send(item))


class TelegramNotifier(Notifier):
    """Telegram Bot API sendMessage 래퍼."""

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        rate_limit_delay: float = 0.5,
        disable_web_page_preview: bool = False,
        timeout: float = 20.0,
    ) -> None:
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID") or ""
        if not self.bot_token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경 변수가 필요합니다")

        self.rate_limit_delay = rate_limit_delay
        self.disable_web_page_preview = disable_web_page_preview
        self.timeout = timeout
        self._last_sent_at = 0.0

    @property
    def _endpoint(self) -> str:
        return f"{API_BASE}/bot{self.bot_token}/sendMessage"

    def send(self, item: ProcessedItem) -> bool:
        self._throttle()
        payload = {
            "chat_id": self.chat_id,
            "text": format_message(item),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": self.disable_web_page_preview,
        }

        try:
            response = httpx.post(self._endpoint, json=payload, timeout=self.timeout)
            if response.status_code == 429:
                retry_after = int(response.json().get("parameters", {}).get("retry_after", 3))
                logger.warning("Telegram 레이트 리밋, %d초 후 재시도", retry_after)
                time.sleep(retry_after)
                response = httpx.post(self._endpoint, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 토큰이 노출되지 않도록 응답 본문만 남긴다.
            logger.error("Telegram 전송 실패 (%s): %s", exc.response.status_code, exc.response.text)
            return False
        except httpx.HTTPError as exc:
            logger.error("Telegram 전송 실패: %s", exc)
            return False

        self._last_sent_at = time.monotonic()
        return True

    def _throttle(self) -> None:
        if not self.rate_limit_delay or not self._last_sent_at:
            return
        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)


class ConsoleNotifier(Notifier):
    """드라이런용. 실제로 보내지 않고 stdout에 출력한다."""

    def send(self, item: ProcessedItem) -> bool:
        print("-" * 60)
        print(format_message(item))
        return True


def build_notifier(
    channel: str = "telegram",
    dry_run: bool = False,
    rate_limit_delay: float = 0.5,
    disable_web_page_preview: bool = False,
) -> Notifier:
    if dry_run:
        return ConsoleNotifier()
    if channel == "telegram":
        return TelegramNotifier(
            rate_limit_delay=rate_limit_delay,
            disable_web_page_preview=disable_web_page_preview,
        )
    raise ValueError(f"지원하지 않는 알림 채널입니다: {channel}")
