"""알림 전송 모듈.

`Notifier` 인터페이스만 지키면 채널을 얼마든지 붙일 수 있고, 여러 채널로
동시에 보낼 수도 있다. 자격 증명이 없는 채널은 예외를 던지는 대신 조용히
빠지므로, 채널 하나가 미설정이라고 파이프라인이 죽지 않는다.

채널별 특성:
    telegram  건당 1메시지, MarkdownV2 (이스케이프 규칙이 까다롭다)
    discord   웹훅 1회에 embed 10개까지 묶어 보낸다
    email     한 번 실행분을 다이제스트 1통으로 묶는다
"""

from __future__ import annotations

import abc
import logging
import os
import smtplib
import time
from datetime import datetime
from email.message import EmailMessage
from html import escape as html_escape
from typing import Iterable, Sequence

import httpx

from .config import EmailConfig, NotifierConfig
from .processor import ProcessedItem

logger = logging.getLogger(__name__)


def _secret(name: str) -> str:
    """비밀 값 환경 변수를 읽는다.

    복사·붙여넣기나 `echo | gh secret set` 과정에서 끝에 개행·공백이 섞여도
    인증이 실패하지 않도록 양끝을 잘라낸다. 값 자체는 그대로 둔다.
    """
    return (os.getenv(name) or "").strip()


API_BASE = "https://api.telegram.org"
DISCORD_EMBEDS_PER_MESSAGE = 10

# Telegram MarkdownV2에서 반드시 이스케이프해야 하는 문자들.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_TABLE = str.maketrans({char: "\\" + char for char in _MDV2_SPECIALS})
_URL_TABLE = str.maketrans({")": "\\)", "\\": "\\\\"})

# 중요도별 Discord embed 색상.
_IMPORTANCE_COLORS = {5: 0xE53935, 4: 0xFB8C00, 3: 0x1E88E5}
_DEFAULT_COLOR = 0x757575


class MissingCredentials(RuntimeError):
    """채널에 필요한 환경 변수가 없을 때. 치명적이지 않다."""


def escape_md(text: str) -> str:
    """MarkdownV2 본문용 이스케이프."""
    return (text or "").translate(_MDV2_TABLE)


def escape_url(url: str) -> str:
    """MarkdownV2 링크 URL용 이스케이프 (본문과 규칙이 다르다)."""
    return (url or "").translate(_URL_TABLE)


def stars(importance: int) -> str:
    return "★" * importance + "☆" * (5 - importance)


def subtitle(item: ProcessedItem) -> str:
    """모든 채널이 공유하는 메타 한 줄."""
    return f"{stars(item.importance)} · {item.insight.category.value} · {item.raw.source}"


def format_message(item: ProcessedItem) -> str:
    """항목 하나를 Telegram MarkdownV2 메시지로 변환한다."""
    lines = [
        f"*{escape_md(item.insight.title_ko)}*",
        escape_md(subtitle(item)),
        "",
    ]
    lines += [f"• {escape_md(sentence)}" for sentence in item.insight.summary_ko]
    if item.raw.url:
        lines += ["", f"[원문 보기]({escape_url(item.raw.url)})"]
    return "\n".join(lines)


class Notifier(abc.ABC):
    """알림 채널 공통 인터페이스."""

    name: str = "notifier"

    @abc.abstractmethod
    def send(self, item: ProcessedItem) -> bool:
        """한 건 전송. 성공 여부를 돌려준다."""

    def send_many(self, items: Sequence[ProcessedItem]) -> int:
        """기본은 건별 전송. 묶어 보내는 채널은 이 메서드를 재정의한다."""
        return sum(1 for item in items if self.send(item))


class _ThrottledNotifier(Notifier):
    """연속 전송 사이에 최소 간격을 두는 채널용 믹스인."""

    def __init__(self, rate_limit_delay: float = 0.5) -> None:
        self.rate_limit_delay = rate_limit_delay
        self._last_sent_at = 0.0

    def _throttle(self) -> None:
        if not self.rate_limit_delay or not self._last_sent_at:
            return
        elapsed = time.monotonic() - self._last_sent_at
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)

    def _mark_sent(self) -> None:
        self._last_sent_at = time.monotonic()


class TelegramNotifier(_ThrottledNotifier):
    """Telegram Bot API sendMessage 래퍼."""

    name = "telegram"

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        rate_limit_delay: float = 0.5,
        disable_web_page_preview: bool = False,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(rate_limit_delay)
        self.bot_token = bot_token or _secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or _secret("TELEGRAM_CHAT_ID")
        if not self.bot_token or not self.chat_id:
            raise MissingCredentials("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 필요")

        self.disable_web_page_preview = disable_web_page_preview
        self.timeout = timeout

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

        self._mark_sent()
        return True


class DiscordNotifier(_ThrottledNotifier):
    """Discord 웹훅. embed를 10개씩 묶어 요청 수를 줄인다."""

    name = "discord"

    def __init__(
        self,
        webhook_url: str | None = None,
        rate_limit_delay: float = 0.5,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(rate_limit_delay)
        self.webhook_url = webhook_url or _secret("DISCORD_WEBHOOK_URL")
        if not self.webhook_url:
            raise MissingCredentials("DISCORD_WEBHOOK_URL 필요")
        self.timeout = timeout

    def send(self, item: ProcessedItem) -> bool:
        return self.send_many([item]) == 1

    def send_many(self, items: Sequence[ProcessedItem]) -> int:
        sent = 0
        for start in range(0, len(items), DISCORD_EMBEDS_PER_MESSAGE):
            batch = items[start : start + DISCORD_EMBEDS_PER_MESSAGE]
            if self._post([_to_embed(item) for item in batch]):
                sent += len(batch)
        return sent

    def _post(self, embeds: list[dict]) -> bool:
        self._throttle()
        try:
            response = httpx.post(
                self.webhook_url, json={"embeds": embeds}, timeout=self.timeout
            )
            if response.status_code == 429:
                retry_after = float(response.json().get("retry_after", 3))
                logger.warning("Discord 레이트 리밋, %.1f초 후 재시도", retry_after)
                time.sleep(retry_after)
                response = httpx.post(
                    self.webhook_url, json={"embeds": embeds}, timeout=self.timeout
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # 웹훅 URL 자체가 비밀이므로 응답 본문만 남긴다.
            logger.error("Discord 전송 실패 (%s): %s", exc.response.status_code, exc.response.text)
            return False
        except httpx.HTTPError as exc:
            logger.error("Discord 전송 실패: %s", exc)
            return False

        self._mark_sent()
        return True


def _to_embed(item: ProcessedItem) -> dict:
    """Discord embed 한 개. 필드 길이 상한은 API 문서 기준으로 자른다."""
    description = "\n".join(f"• {line}" for line in item.insight.summary_ko)
    embed: dict = {
        "title": item.insight.title_ko[:256],
        "description": description[:4096],
        "color": _IMPORTANCE_COLORS.get(item.importance, _DEFAULT_COLOR),
        "footer": {"text": subtitle(item)[:2048]},
    }
    if item.raw.url:
        embed["url"] = item.raw.url
    return embed


class EmailNotifier(Notifier):
    """SMTP 다이제스트. 실행 1회분을 메일 한 통으로 묶어 보낸다."""

    name = "email"

    def __init__(self, config: EmailConfig, timeout: float = 30.0) -> None:
        self.config = config
        self.user = _secret("SMTP_USER")
        self.password = _secret("SMTP_PASSWORD")
        if not self.user or not self.password:
            raise MissingCredentials("SMTP_USER / SMTP_PASSWORD 필요")

        recipients = config.recipients or [self.user]
        self.recipients = recipients
        self.sender = config.sender or self.user
        self.timeout = timeout

    def send(self, item: ProcessedItem) -> bool:
        return self.send_many([item]) == 1

    def send_many(self, items: Sequence[ProcessedItem]) -> int:
        if not items:
            return 0

        message = EmailMessage()
        message["Subject"] = (
            f"{self.config.subject_prefix} {len(items)}건 "
            f"({datetime.now().strftime('%Y-%m-%d %H:%M')})"
        )
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(_plain_digest(items))
        message.add_alternative(_html_digest(items), subtype="html")

        try:
            self._deliver(message)
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("이메일 전송 실패: %s", exc)
            return 0

        logger.info("이메일 다이제스트 발송: %s", ", ".join(self.recipients))
        return len(items)

    def _deliver(self, message: EmailMessage) -> None:
        config = self.config
        # 465는 처음부터 TLS, 587은 접속 후 STARTTLS로 승격한다.
        if config.port == 465:
            with smtplib.SMTP_SSL(config.host, config.port, timeout=self.timeout) as server:
                server.login(self.user, self.password)
                server.send_message(message)
            return

        with smtplib.SMTP(config.host, config.port, timeout=self.timeout) as server:
            if config.use_tls:
                server.starttls()
            server.login(self.user, self.password)
            server.send_message(message)


def _plain_digest(items: Sequence[ProcessedItem]) -> str:
    blocks = []
    for item in items:
        lines = [item.insight.title_ko, subtitle(item), ""]
        lines += [f"- {line}" for line in item.insight.summary_ko]
        if item.raw.url:
            lines += ["", item.raw.url]
        blocks.append("\n".join(lines))
    return ("\n\n" + "-" * 50 + "\n\n").join(blocks)


def _html_digest(items: Sequence[ProcessedItem]) -> str:
    cards = []
    for item in items:
        summary = "".join(
            f"<li style='margin:4px 0'>{html_escape(line)}</li>"
            for line in item.insight.summary_ko
        )
        title = html_escape(item.insight.title_ko)
        heading = (
            f"<a href='{html_escape(item.raw.url)}' style='color:#1a73e8;"
            f"text-decoration:none'>{title}</a>"
            if item.raw.url
            else title
        )
        cards.append(
            "<div style='margin:0 0 28px;padding:0 0 20px;"
            "border-bottom:1px solid #e8e8e8'>"
            f"<h3 style='margin:0 0 6px;font-size:17px'>{heading}</h3>"
            f"<div style='color:#777;font-size:13px;margin-bottom:10px'>"
            f"{html_escape(subtitle(item))}</div>"
            f"<ul style='margin:0;padding-left:20px;font-size:14px;"
            f"line-height:1.6'>{summary}</ul>"
            "</div>"
        )
    return (
        "<div style='max-width:680px;margin:0 auto;font-family:"
        "-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;color:#222'>"
        + "".join(cards)
        + "</div>"
    )


class ConsoleNotifier(Notifier):
    """드라이런용. 실제로 보내지 않고 stdout에 출력한다."""

    name = "console"

    def send(self, item: ProcessedItem) -> bool:
        print("-" * 60)
        print(format_message(item))
        return True


def build_notifiers(config: NotifierConfig, dry_run: bool = False) -> list[Notifier]:
    """설정에 적힌 채널 중 자격 증명이 갖춰진 것만 만든다.

    쓸 수 있는 채널이 하나도 없으면 콘솔로 떨어뜨린다. 알림 채널 미설정 때문에
    이미 끝낸 LLM 작업을 통째로 잃는 일이 없어야 하기 때문이다.
    """
    if dry_run:
        return [ConsoleNotifier()]

    notifiers: list[Notifier] = []
    for channel in config.channels:
        try:
            notifiers.append(_build_one(channel, config))
        except MissingCredentials as exc:
            logger.warning("'%s' 채널 건너뜀: %s", channel, exc)

    if not notifiers:
        logger.warning("사용 가능한 알림 채널이 없어 콘솔로 출력합니다")
        return [ConsoleNotifier()]
    return notifiers


def _build_one(channel: str, config: NotifierConfig) -> Notifier:
    if channel == "telegram":
        return TelegramNotifier(
            rate_limit_delay=config.rate_limit_delay,
            disable_web_page_preview=config.disable_web_page_preview,
        )
    if channel == "discord":
        return DiscordNotifier(rate_limit_delay=config.rate_limit_delay)
    if channel == "email":
        return EmailNotifier(config.email)
    raise ValueError(f"지원하지 않는 알림 채널입니다: {channel}")
