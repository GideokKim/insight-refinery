"""중복 판정.

ID·URL·제목 세 기준을 각각 확인한다. 실제로 겪은 경로를 그대로 재현한다:
TechCrunch 기사가 Hacker News에 다시 올라오고, 같은 사건을 다른 매체가 쓴다.
"""

from __future__ import annotations

import json

from core.dedup import ProcessedStore, normalize_title, normalize_url


class TestNormalizeUrl:
    def test_ignores_scheme_www_and_trailing_slash(self):
        canonical = "techcrunch.com/2026/08/17/foo"
        assert normalize_url("https://www.techcrunch.com/2026/08/17/foo/") == canonical
        assert normalize_url("http://techcrunch.com/2026/08/17/foo") == canonical

    def test_drops_tracking_parameters(self):
        assert (
            normalize_url("https://a.com/p?utm_source=hn&utm_medium=x&ref=y") == "a.com/p"
        )

    def test_keeps_meaningful_parameters_in_stable_order(self):
        assert normalize_url("https://a.com/p?b=2&a=1") == "a.com/p?a=1&b=2"

    def test_empty_url_is_empty(self):
        assert normalize_url("") == ""


class TestNormalizeTitle:
    def test_folds_case_and_punctuation(self):
        assert normalize_title("Anthropic's Revenue: $65B!") == "anthropic s revenue 65b"

    def test_collapses_whitespace(self):
        assert normalize_title("  a   b\n c ") == "a b c"


class TestFindDuplicate:
    def test_same_id_is_duplicate(self, tmp_path, raw_item):
        store = ProcessedStore(tmp_path / "c.json").load()
        item = raw_item()
        assert store.find_duplicate(item) is None
        store.add(item)
        assert store.find_duplicate(item) == "동일 ID"

    def test_same_article_from_another_source(self, tmp_path, raw_item):
        """TechCrunch 기사가 Hacker News에 올라온 경우.

        HN 항목의 ID는 news.ycombinator.com 링크라 ID로는 안 걸린다.
        """
        store = ProcessedStore(tmp_path / "c.json").load()
        article = "https://techcrunch.com/2026/08/17/anthropic-revenue/"
        store.add(
            raw_item(
                external_id="https://techcrunch.com/?p=3153830",
                title="Anthropic revenue surges",
                url=article,
            )
        )

        from_hn = raw_item(
            external_id="https://news.ycombinator.com/item?id=49353432",
            title="Anthropic revenue surges",
            url=article + "?utm_source=hn",
        )
        assert store.find_duplicate(from_hn).startswith("동일 URL")

    def test_same_event_written_by_another_outlet(self, tmp_path, raw_item):
        store = ProcessedStore(tmp_path / "c.json").load()
        store.add(
            raw_item(
                external_id="tc-1",
                title="Anthropic's annualized revenue surges to $65B",
                url="https://techcrunch.com/a",
            )
        )
        rival = raw_item(
            external_id="vb-1",
            title="Anthropic's annualized revenue surges to $65 B",
            url="https://venturebeat.com/b",
        )
        assert store.find_duplicate(rival).startswith("유사 제목")

    def test_unrelated_news_passes(self, tmp_path, raw_item):
        """오탐 확인. 같은 회사 얘기라도 다른 사건이면 통과해야 한다."""
        store = ProcessedStore(tmp_path / "c.json").load()
        store.add(
            raw_item(
                external_id="1",
                title="Anthropic's annualized revenue surges to $65B",
                url="https://a.com/1",
            )
        )
        for title in (
            "Anthropic shares details about Claude's new watermarking",
            "OpenAI launches ChatGPT for Teens",
            "Nvidia investing $1.5B in SoftBank data center",
        ):
            item = raw_item(external_id=title, title=title, url=f"https://x.com/{title}")
            assert store.find_duplicate(item) is None, title

    def test_threshold_zero_disables_title_matching_only(self, tmp_path, raw_item):
        store = ProcessedStore(tmp_path / "c.json", similarity_threshold=0).load()
        store.add(raw_item(external_id="1", title="같은 제목", url="https://a.com/1"))

        similar = raw_item(external_id="2", title="같은 제목", url="https://b.com/2")
        assert store.find_duplicate(similar) is None

        same_url = raw_item(external_id="3", title="다른 제목", url="https://a.com/1")
        assert store.find_duplicate(same_url).startswith("동일 URL")

    def test_stage_catches_duplicates_within_one_run(self, tmp_path, raw_item):
        """요약 전이라 저장은 못 하지만, 뒤따르는 아이템과는 비교돼야 한다."""
        store = ProcessedStore(tmp_path / "c.json").load()
        first = raw_item(external_id="1", url="https://a.com/x")
        store.stage(first)

        assert store.find_duplicate(raw_item(external_id="2", url="https://a.com/x"))
        assert len(store) == 0, "stage는 저장하지 않아야 한다"
        assert store.save() is False, "stage만으로 파일을 쓰면 안 된다"


class TestPersistence:
    def test_roundtrip(self, tmp_path, raw_item):
        path = tmp_path / "c.json"
        store = ProcessedStore(path).load()
        store.add(raw_item(external_id="1", url="https://a.com/1"))
        assert store.save() is True
        assert store.save() is False, "변경이 없으면 다시 쓰지 않는다"

        reloaded = ProcessedStore(path).load()
        assert len(reloaded) == 1
        assert reloaded.find_duplicate(
            raw_item(external_id="other", url="https://www.a.com/1/?utm_source=x")
        )

    def test_reads_v1_files(self, tmp_path, raw_item):
        """v1은 값이 타임스탬프 문자열이었다. URL·제목이 없으니 ID로만 걸린다."""
        path = tmp_path / "c.json"
        path.write_text(
            json.dumps({"version": 1, "ids": {"rss:old": "2026-08-19T12:00:00+00:00"}}),
            encoding="utf-8",
        )
        store = ProcessedStore(path).load()
        assert len(store) == 1
        assert store.find_duplicate(raw_item(external_id="old")) == "동일 ID"

        store.add(raw_item(external_id="new", url="https://new.com/a"))
        store.save()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        assert payload["ids"]["rss:new"]["url"] == "new.com/a"

    def test_broken_file_does_not_stop_the_run(self, tmp_path):
        path = tmp_path / "c.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(ProcessedStore(path).load()) == 0

    def test_prune_drops_oldest_first(self, tmp_path, raw_item):
        from datetime import datetime, timezone

        path = tmp_path / "c.json"
        store = ProcessedStore(path, max_entries=2).load()
        for index, day in enumerate((17, 18, 19), start=1):
            store.add(
                raw_item(external_id=str(index), url=f"https://a.com/{index}"),
                when=datetime(2026, 8, day, tzinfo=timezone.utc),
            )
        store.save()

        kept = ProcessedStore(path).load()
        assert len(kept) == 2
        assert "rss:1" not in kept
        assert "rss:3" in kept
