"""LLM provider 체인.

OpenAI 클라이언트는 전부 대역으로 바꾼다. 네트워크도 API 키도 쓰지 않는다.
"""

from __future__ import annotations

import types

import pytest

import core.processor as processor_module
from core.config import ProviderConfig
from core.processor import Category, Insight, Processor, _retry_delay

GEMINI = ProviderConfig(
    name="gemini", model="gemini-3.5-flash-lite",
    api_key_env="GEMINI_API_KEY", base_url="https://gemini/",
)
GROQ = ProviderConfig(
    name="groq", model="openai/gpt-oss-20b",
    api_key_env="GROQ_API_KEY", base_url="https://groq/",
)

GOOD = Insight(
    title_ko="제목", summary_ko=["a", "b", "c"], category=Category.LLM, importance=4
)
JSON_REPLY = (
    '```json\n{"title_ko":"제목","summary_ko":["a","b","c"],'
    '"category":"LLM","importance":4}\n```'
)


class FakeBadRequest(Exception):
    pass


class FakeRateLimit(Exception):
    def __init__(self, message="429", headers=None):
        super().__init__(message)
        self.response = types.SimpleNamespace(headers=headers or {})


def _message(parsed=None, content=None, refusal=None):
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(
                    parsed=parsed, content=content, refusal=refusal
                )
            )
        ]
    )


class FakeClient:
    """behaviour: ok | boom | schema_reject | rate_limited_once"""

    behaviours: dict[str, str] = {}

    def __init__(self, api_key=None, base_url=None, **kwargs):
        assert kwargs.get("max_retries") == 0, "SDK 내장 재시도는 꺼야 한다"
        self.behaviour = FakeClient.behaviours.get(base_url, "ok")
        self.calls: list[tuple[str, object]] = []
        outer = self

        class Completions:
            def parse(self, **kw):
                outer.calls.append(("parse", kw.get("model")))
                if outer.behaviour == "boom":
                    raise RuntimeError("quota exceeded")
                if outer.behaviour == "schema_reject":
                    raise FakeBadRequest("json_schema unsupported")
                if outer.behaviour == "rate_limited_once":
                    outer.behaviour = "ok"
                    raise FakeRateLimit("Please retry in 11.98s.")
                return _message(parsed=GOOD.model_copy())

            def create(self, **kw):
                fmt = kw.get("response_format", {})
                outer.calls.append(("create", fmt.get("type")))
                if outer.behaviour == "boom":
                    raise RuntimeError("quota exceeded")
                return _message(content=JSON_REPLY)

        self.chat = types.SimpleNamespace(completions=Completions())
        self.beta = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=Completions())
        )


@pytest.fixture(autouse=True)
def fake_openai(monkeypatch):
    FakeClient.behaviours = {}
    monkeypatch.setattr(processor_module, "OpenAI", FakeClient)
    monkeypatch.setattr(processor_module, "BadRequestError", FakeBadRequest)
    monkeypatch.setattr(processor_module, "RateLimitError", FakeRateLimit)
    monkeypatch.setattr(processor_module.time, "sleep", lambda _: None)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GROQ_API_KEY", "k")


class TestProviderChain:
    def test_skips_providers_without_a_key(self, monkeypatch, raw_item):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "k")
        result = Processor([GEMINI, GROQ]).process(raw_item())
        assert result.provider == "groq"

    def test_falls_back_when_the_first_provider_fails(self, keys, raw_item):
        FakeClient.behaviours = {"https://gemini/": "boom"}
        assert Processor([GEMINI, GROQ]).process(raw_item()).provider == "groq"

    def test_returns_none_when_every_provider_fails(self, keys, raw_item):
        FakeClient.behaviours = {"https://gemini/": "boom", "https://groq/": "boom"}
        assert Processor([GEMINI, GROQ], max_retries=1).process(raw_item()) is None

    def test_without_any_key_it_fails_at_startup(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            Processor([GEMINI, GROQ])

    def test_trims_whitespace_around_keys(self, monkeypatch, raw_item):
        monkeypatch.setenv("GEMINI_API_KEY", "  k\n")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert Processor([GEMINI, GROQ]).process(raw_item()) is not None

    def test_blank_key_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "   ")
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        with pytest.raises(RuntimeError):
            Processor([GEMINI, GROQ])


class TestStructuredOutput:
    def test_downgrades_when_strict_schema_is_refused(self, keys, raw_item):
        FakeClient.behaviours = {"https://gemini/": "schema_reject"}
        proc = Processor([GEMINI, GROQ])
        result = proc.process(raw_item())

        assert result.provider == "gemini"
        provider = proc._providers[0]
        assert provider.mode == "json_object", "강등이 유지돼야 다음 건에 400을 또 맞지 않는다"
        assert provider.client.calls == [
            ("parse", "gemini-3.5-flash-lite"),
            ("create", "json_object"),
        ]

    def test_strips_code_fences_in_json_mode(self, keys, raw_item):
        FakeClient.behaviours = {"https://gemini/": "schema_reject"}
        result = Processor([GEMINI]).process(raw_item())
        assert result.insight.summary_ko == ["a", "b", "c"]

    def test_summary_is_trimmed_to_three_lines(self):
        from core.processor import _normalize_summary

        assert _normalize_summary(["- a", "• b", "", " c ", "d"]) == ["a", "b", "c"]


class TestRateLimit:
    def test_waits_for_the_delay_the_response_names(self, keys, raw_item, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(processor_module.time, "sleep", slept.append)
        FakeClient.behaviours = {"https://gemini/": "rate_limited_once"}

        assert Processor([GEMINI]).process(raw_item()) is not None
        assert slept == [12.98], "서버가 알려준 11.98초 + 여유 1초"

    @pytest.mark.parametrize(
        "exc, expected",
        [
            (FakeRateLimit("Please retry in 11.98s."), 12.98),
            (FakeRateLimit("x", {"retry-after": "7"}), 7.0),
            (FakeRateLimit("'retryDelay': '9s'"), 10.0),
            (FakeRateLimit("Please retry in 999s."), 60.0),  # 상한
            (FakeRateLimit("알 수 없음"), 4.0),  # 폴백
        ],
    )
    def test_delay_is_read_from_the_response(self, exc, expected):
        assert _retry_delay(exc, fallback=4.0, cap=60.0) == expected
