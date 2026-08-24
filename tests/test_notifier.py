"""알림 채널.

HTTP와 SMTP는 대역으로 바꾼다. 실제로 무언가 발송되는 일은 없다.
"""

from __future__ import annotations

import types

import pytest

import core.notifier as notifier_module
from core.config import EmailConfig, NotifierConfig
from core.notifier import (
    ConsoleNotifier,
    DiscordNotifier,
    EmailNotifier,
    MissingCredentials,
    TelegramNotifier,
    build_notifiers,
    escape_md,
    escape_url,
    format_message,
)


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "DISCORD_WEBHOOK_URL", "SMTP_USER", "SMTP_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def captured_posts(monkeypatch):
    posts: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json)
        return types.SimpleNamespace(status_code=204, raise_for_status=lambda: None)

    monkeypatch.setattr(notifier_module.httpx, "post", fake_post)
    return posts


class TestTelegramFormatting:
    def test_escapes_markdown_v2_specials(self, processed, insight):
        item = processed(1)
        item.insight = insight(title_ko="가격 인하!", summary_ko=["첫째. 중요!", "둘째-줄"])
        message = format_message(item)
        assert "\\!" in message and "\\." in message and "\\-" in message

    def test_link_urls_follow_a_different_rule(self):
        """본문과 달리 링크 안에서는 ')' 와 '\\' 만 이스케이프 대상이다."""
        assert escape_url("https://x.com/a(1)") == "https://x.com/a(1\\)"
        assert escape_md("a_b") == "a\\_b"

    def test_message_carries_title_meta_and_link(self, processed):
        message = format_message(processed(1, importance=4))
        assert message.startswith("*한국어 제목*")
        assert "★★★★☆" in message
        assert "[원문 보기](https://example.com/1)" in message


class TestDiscord:
    def test_batches_ten_embeds_per_request(self, captured_posts, processed, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/webhook")
        items = [processed(i) for i in range(23)]

        assert DiscordNotifier(rate_limit_delay=0).send_many(items) == 23
        assert [len(post["embeds"]) for post in captured_posts] == [10, 10, 3]

    def test_embed_keeps_text_unescaped(self, captured_posts, processed, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/webhook")
        DiscordNotifier(rate_limit_delay=0).send_many([processed(1)])

        embed = captured_posts[0]["embeds"][0]
        assert embed["description"] == "• 첫째 줄\n• 둘째 줄\n• 셋째 줄"
        assert embed["url"] == "https://example.com/1"
        assert "★★★★☆" in embed["footer"]["text"]

    def test_colour_tracks_importance(self, captured_posts, processed, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/webhook")
        notifier = DiscordNotifier(rate_limit_delay=0)
        notifier.send_many([processed(1, importance=5), processed(2, importance=1)])

        colours = [embed["color"] for embed in captured_posts[0]["embeds"]]
        assert colours[0] != colours[1]

    def test_requires_a_webhook_url(self):
        with pytest.raises(MissingCredentials, match="DISCORD_WEBHOOK_URL"):
            DiscordNotifier()


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.message = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, message):
        self.message = message


class TestEmail:
    @pytest.fixture(autouse=True)
    def smtp(self, monkeypatch):
        FakeSMTP.instances = []
        monkeypatch.setattr(notifier_module.smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(notifier_module.smtplib, "SMTP_SSL", FakeSMTP)
        monkeypatch.setenv("SMTP_USER", "me@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password")

    def test_collapses_a_run_into_one_message(self, processed):
        items = [processed(i) for i in range(23)]
        assert EmailNotifier(EmailConfig()).send_many(items) == 23

        message = FakeSMTP.instances[0].message
        assert "23건" in message["Subject"]
        assert message["To"] == "me@gmail.com", "수신자를 비우면 본인에게 보낸다"

    def test_sends_html_with_a_plain_text_alternative(self, processed):
        EmailNotifier(EmailConfig()).send_many([processed(1)])
        message = FakeSMTP.instances[0].message
        assert message.get_body(("html",)) is not None
        assert message.get_body(("plain",)) is not None

    def test_uses_starttls_on_587(self, processed):
        EmailNotifier(EmailConfig()).send_many([processed(1)])
        assert (FakeSMTP.instances[0].host, FakeSMTP.instances[0].port) == (
            "smtp.gmail.com", 587,
        )
        assert FakeSMTP.instances[0].started_tls

    def test_uses_implicit_tls_on_465(self, processed):
        EmailNotifier(EmailConfig(port=465)).send_many([processed(1)])
        assert FakeSMTP.instances[0].port == 465
        assert not FakeSMTP.instances[0].started_tls

    def test_joins_multiple_recipients(self, processed):
        config = EmailConfig(recipients=["a@b.com", "c@d.com"])
        EmailNotifier(config).send_many([processed(1)])
        assert FakeSMTP.instances[0].message["To"] == "a@b.com, c@d.com"

    def test_trims_whitespace_around_credentials(self, monkeypatch, processed):
        """`echo | gh secret set` 으로 넣으면 끝에 개행이 붙는다."""
        monkeypatch.setenv("SMTP_USER", " me@gmail.com\n")
        monkeypatch.setenv("SMTP_PASSWORD", "app-password\n")
        notifier = EmailNotifier(EmailConfig())
        assert notifier.user == "me@gmail.com"
        assert notifier.password == "app-password"

    def test_keeps_inner_spaces_in_passwords(self, monkeypatch):
        monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
        assert EmailNotifier(EmailConfig()).password == "abcd efgh ijkl mnop"

    def test_requires_credentials(self, monkeypatch):
        monkeypatch.delenv("SMTP_USER", raising=False)
        with pytest.raises(MissingCredentials, match="SMTP_USER"):
            EmailNotifier(EmailConfig())


class TestBuildNotifiers:
    def test_skips_channels_without_credentials(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/webhook")
        config = NotifierConfig(channels=["discord", "email", "telegram"])
        assert [n.name for n in build_notifiers(config)] == ["discord"]

    def test_falls_back_to_console_when_nothing_is_configured(self):
        """알림 설정이 없다고 이미 끝낸 요약을 버릴 이유는 없다."""
        config = NotifierConfig(channels=["discord", "email"])
        assert [type(n) for n in build_notifiers(config)] == [ConsoleNotifier]

    def test_dry_run_never_sends(self, monkeypatch):
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord/webhook")
        config = NotifierConfig(channels=["discord"])
        assert [type(n) for n in build_notifiers(config, dry_run=True)] == [
            ConsoleNotifier
        ]

    def test_wraps_email_in_a_digest_when_an_hour_is_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SMTP_USER", "me@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "pw")
        monkeypatch.setattr(notifier_module.smtplib, "SMTP", FakeSMTP)
        config = NotifierConfig(
            channels=["email"],
            email=EmailConfig(digest_hour=23, queue_path=tmp_path / "q.json"),
        )
        built = build_notifiers(config)
        assert isinstance(built[0], notifier_module.DigestNotifier)
        assert built[0].name == "email"

    def test_telegram_needs_both_token_and_chat_id(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        with pytest.raises(MissingCredentials):
            TelegramNotifier()


class TestThresholds:
    def test_channel_overrides_win(self):
        config = NotifierConfig(min_importance={"discord": 3, "email": 4})
        assert config.threshold_for("discord", 99) == 3
        assert config.threshold_for("email", 99) == 4

    def test_unlisted_channels_use_the_default(self):
        config = NotifierConfig(min_importance={"discord": 3})
        assert config.threshold_for("telegram", 3) == 3
