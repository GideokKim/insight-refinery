"""설정 파일과 파이프라인 조립.

`config.yaml`을 실제로 읽어 검증하므로, 설정을 잘못 고치면 여기서 걸린다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

import main as main_module
from core.config import Config, load_config
from core.dedup import ProcessedStore
from core.notifier import Notifier

ROOT = Path(__file__).resolve().parent.parent


class TestShippedConfig:
    @pytest.fixture(scope="class")
    def config(self) -> Config:
        return load_config(ROOT / "config.yaml")

    def test_it_loads(self, config):
        assert config.sources, "소스가 하나도 없다"
        assert config.llm.providers

    def test_every_rss_source_has_a_url(self, config):
        for source in config.sources:
            if source.type == "rss":
                assert source.options.get("url"), source.name

    def test_source_names_are_unique(self, config):
        names = [source.name for source in config.sources]
        assert len(names) == len(set(names))

    def test_digest_hour_is_one_of_the_scheduled_hours(self, config):
        """cron이 돌지 않는 시각을 적으면 다이제스트가 영영 나가지 않는다."""
        digest_hour = config.notifier.email.digest_hour
        if digest_hour is None:
            pytest.skip("매 실행 발송 설정")

        workflow = yaml.safe_load((ROOT / ".github/workflows/pipeline.yml").read_text())
        # PyYAML은 `on:` 을 불리언 True로 읽는다.
        schedule = workflow.get("on", workflow.get(True))["schedule"]
        hours = {
            int(part)
            for entry in schedule
            for part in entry["cron"].split()[1].split(",")
            if part.isdigit()
        }
        assert digest_hour in hours, f"digest_hour={digest_hour}, cron 시각={sorted(hours)}"

    def test_notifier_thresholds_name_enabled_channels(self, config):
        for channel in config.notifier.min_importance:
            assert channel in config.notifier.channels, channel


class TestConfigValidation:
    def test_rejects_out_of_range_importance(self):
        with pytest.raises(Exception):
            Config.model_validate({"run": {"min_importance": 9}})

    def test_rejects_unknown_keys(self):
        with pytest.raises(Exception):
            Config.model_validate({"run": {"typo_here": 1}})

    def test_defaults_stand_alone(self):
        config = Config()
        assert [p.name for p in config.llm.providers] == ["gemini", "groq"]


class TestFilterNew:
    def test_drops_duplicates_within_one_run(self, tmp_path, raw_item):
        store = ProcessedStore(tmp_path / "c.json").load()
        shared = "https://techcrunch.com/a"
        items = [
            raw_item(external_id="tc", url=shared),
            raw_item(external_id="hn", url=shared + "?utm_source=hn"),
            raw_item(external_id="other", title="다른 뉴스", url="https://b.com/1"),
        ]
        fresh = main_module.filter_new(items, store)
        assert [item.external_id for item in fresh] == ["tc", "other"]

    def test_respects_the_persisted_cache(self, tmp_path, raw_item):
        store = ProcessedStore(tmp_path / "c.json").load()
        seen = raw_item(external_id="seen")
        store.add(seen)
        assert main_module.filter_new([seen], store) == []


class RecordingNotifier(Notifier):
    def __init__(self, name: str) -> None:
        self.name = name
        self.received: list[int] = []

    def send(self, item):
        return True

    def send_many(self, items):
        self.received.append(len(items))
        return len(items)


class TestNotifyStep:
    """21일 아침 사고: 이번 실행 해당분이 0건이면 채널을 통째로 건너뛰었다."""

    @staticmethod
    def _config():
        return Config.model_validate({
            "run": {"min_importance": 3},
            "notifier": {
                "channels": ["discord", "email"],
                "min_importance": {"discord": 3, "email": 4},
            },
            "sources": [],
        })

    def test_each_channel_filters_by_its_own_threshold(self, monkeypatch, processed):
        discord, email = RecordingNotifier("discord"), RecordingNotifier("email")
        monkeypatch.setattr(main_module, "build_notifiers", lambda *a, **k: [discord, email])

        items = [processed(i, importance=(i % 5) + 1) for i in range(10)]
        main_module._notify(
            self._config(), argparse.Namespace(dry_run=False, send_digest=False), items
        )
        assert discord.received == [6]
        assert email.received == [4]

    def test_channels_are_called_even_with_nothing_to_send(self, monkeypatch, processed):
        discord, email = RecordingNotifier("discord"), RecordingNotifier("email")
        monkeypatch.setattr(main_module, "build_notifiers", lambda *a, **k: [discord, email])

        low = [processed(i, importance=3) for i in range(3)]
        main_module._notify(
            self._config(), argparse.Namespace(dry_run=False, send_digest=False), low
        )
        assert discord.received == [3]
        assert email.received == [0], "해당분이 없어도 호출은 돼야 대기열이 비워진다"
