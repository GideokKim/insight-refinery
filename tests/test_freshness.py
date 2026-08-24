"""오래된 항목 걸러내기.

피드는 최신순 N개를 준다. 게시 빈도가 낮은 블로그는 그 N개가 몇 달 전까지 닿아서,
소스를 새로 켜면 과거분이 통째로 "신규"로 들어온다. 실제로 54일 지난 글이
알림으로 나갔다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import main as main_module
from core.config import Config
from main import _drop_stale

UTC = timezone.utc
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _aged(raw_item, days: float):
    return raw_item(
        external_id=f"age-{days}",
        published_at=NOW - timedelta(days=days),
    )


class TestDropStale:
    def test_keeps_recent_items(self, raw_item):
        items = [_aged(raw_item, 0), _aged(raw_item, 1), _aged(raw_item, 2.9)]
        kept, dropped = _drop_stale(items, 3, NOW)
        assert len(kept) == 3 and dropped == 0

    def test_drops_items_past_the_window(self, raw_item):
        items = [_aged(raw_item, 1), _aged(raw_item, 54), _aged(raw_item, 23)]
        kept, dropped = _drop_stale(items, 3, NOW)
        assert [item.external_id for item in kept] == ["age-1"]
        assert dropped == 2

    def test_boundary_is_inclusive(self, raw_item):
        kept, dropped = _drop_stale([_aged(raw_item, 3)], 3, NOW)
        assert dropped == 0, "정확히 경계에 걸친 항목은 남긴다"

    def test_keeps_items_without_a_date(self, raw_item):
        """날짜를 안 주는 피드를 통째로 잃는 것이 더 나쁘다."""
        items = [raw_item(external_id="undated", published_at=None)]
        kept, dropped = _drop_stale(items, 3, NOW)
        assert len(kept) == 1 and dropped == 0

    @pytest.mark.parametrize("disabled", [None, 0])
    def test_disabled_keeps_everything(self, raw_item, disabled):
        items = [_aged(raw_item, 54), _aged(raw_item, 1)]
        kept, dropped = _drop_stale(items, disabled, NOW)
        assert len(kept) == 2 and dropped == 0


class TestCollectIntegration:
    """소스별 override가 먹는지. 주간 뉴스레터는 3일로 자르면 매주 놓친다."""

    @staticmethod
    def _config(**run):
        return Config.model_validate({
            "run": run,
            "sources": [
                {"name": "일간", "type": "rss", "options": {"url": "u"}},
                {"name": "주간", "type": "rss",
                 "options": {"url": "u", "max_age_days": 8}},
            ],
        })

    def test_per_source_override(self, monkeypatch, raw_item):
        five_days_old = [
            raw_item(external_id="a", published_at=datetime.now(UTC) - timedelta(days=5))
        ]

        def fake_build(sources):
            built = []
            for source in sources:
                collector = type(
                    "Fake", (),
                    {
                        "name": source.name,
                        "options": source.options,
                        "safe_collect": lambda self: list(five_days_old),
                    },
                )()
                built.append(collector)
            return built

        monkeypatch.setattr(main_module, "build_collectors", fake_build)
        collected = main_module.collect(self._config(max_age_days=3), None)

        assert len(collected) == 1, "일간 소스는 5일 지난 글을 버려야 한다"

    def test_newest_first_ordering_survives(self, monkeypatch, raw_item):
        now = datetime.now(UTC)
        items = [
            raw_item(external_id="old", published_at=now - timedelta(hours=10)),
            raw_item(external_id="new", published_at=now - timedelta(hours=1)),
            raw_item(external_id="undated", published_at=None),
        ]
        monkeypatch.setattr(
            main_module, "build_collectors",
            lambda sources: [
                type("Fake", (), {
                    "name": "s", "options": {}, "safe_collect": lambda self: list(items)
                })()
            ],
        )
        ordered = main_module.collect(self._config(max_age_days=3), None)
        assert [item.external_id for item in ordered] == ["new", "old", "undated"]


class TestShippedConfig:
    def test_freshness_window_is_set(self):
        from pathlib import Path

        from core.config import load_config

        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
        assert config.run.max_age_days, "이 값이 없으면 소스 추가 때마다 과거분이 쏟아진다"

    def test_weekly_sources_get_a_wider_window(self):
        from pathlib import Path

        from core.config import load_config

        config = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
        default = config.run.max_age_days
        for source in config.sources:
            override = source.options.get("max_age_days")
            if override is not None:
                assert override > default, source.name
