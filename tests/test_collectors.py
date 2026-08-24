"""수집기.

네트워크는 쓰지 않는다. 피드는 임시 파일로, 거절 응답은 대역으로 만든다.
"""

from __future__ import annotations

import types
from datetime import timezone

import pytest

from core.collectors import build_collectors
from core.collectors.base import Collector
from core.collectors.rss import RSSCollector, _retry_after
from core.config import SourceConfig

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item>
  <title>GPT-4o mini &amp; RAG</title>
  <link>https://example.com/posts/1</link>
  <guid>https://example.com/posts/1</guid>
  <pubDate>Mon, 18 Aug 2025 09:00:00 GMT</pubDate>
  <author>jane@example.com</author>
  <description>&lt;p&gt;Hello   &lt;b&gt;world&lt;/b&gt;.&lt;/p&gt;</description>
</item>
<item>
  <title>Older post</title>
  <link>https://example.com/posts/0</link>
  <guid>https://example.com/posts/0</guid>
  <pubDate>Sun, 17 Aug 2025 09:00:00 GMT</pubDate>
  <description>old</description>
</item>
<item><title></title><link></link><guid></guid></item>
</channel></rss>
"""


@pytest.fixture
def feed_path(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_text(FEED, encoding="utf-8")
    return str(path)


class TestParsing:
    def test_reads_entries(self, feed_path):
        items = list(RSSCollector("T", {"url": feed_path}).collect())
        assert len(items) == 2, "제목·링크가 없는 엔트리는 버려야 한다"
        assert items[0].title == "GPT-4o mini & RAG"
        assert items[0].external_id == "https://example.com/posts/1"
        assert items[0].author == "jane@example.com"

    def test_strips_html_and_collapses_space(self, feed_path):
        item = next(iter(RSSCollector("T", {"url": feed_path}).collect()))
        assert item.content == "Hello world ."

    def test_published_at_is_utc(self, feed_path):
        """`time.mktime`은 struct_time을 로컬 시각으로 읽어 값을 밀어버린다."""
        item = next(iter(RSSCollector("T", {"url": feed_path}).collect()))
        assert item.published_at.isoformat() == "2025-08-18T09:00:00+00:00"
        assert item.published_at.tzinfo is timezone.utc

    def test_limit(self, feed_path):
        assert len(list(RSSCollector("T", {"url": feed_path, "limit": 1}).collect())) == 1

    def test_dedup_key_is_namespaced(self, feed_path):
        item = next(iter(RSSCollector("T", {"url": feed_path}).collect()))
        assert item.dedup_key == "rss:https://example.com/posts/1"

    def test_missing_url_option(self):
        with pytest.raises(ValueError, match="url"):
            list(RSSCollector("T", {}).collect())


class TestFetchRetry:
    """Reddit은 연속 요청에 429를 뱉지만 몇 초 뒤엔 통과한다."""

    @staticmethod
    def _feed(status, entries=()):
        return types.SimpleNamespace(
            status=status, entries=list(entries), bozo=False, headers={},
            get=lambda key, default=None: default,
        )

    @pytest.fixture
    def collector(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("core.collectors.rss.time.sleep", slept.append)
        made = RSSCollector("T", {"url": "u", "retries": 3, "retry_delay": 5})
        made.slept = slept
        return made

    def _serve(self, monkeypatch, responses):
        queue = list(responses)
        seen: list[str | None] = []

        def fake_parse(url, agent=None):
            seen.append(agent)
            return queue.pop(0)

        monkeypatch.setattr("core.collectors.rss.feedparser.parse", fake_parse)
        return seen

    def test_retries_then_succeeds(self, collector, monkeypatch):
        self._serve(
            monkeypatch,
            [self._feed(429), self._feed(429), self._feed(200, [1, 2])],
        )
        assert len(collector._fetch("u").entries) == 2
        assert collector.slept == [5.0, 10.0], "대기 시간이 늘어나야 한다"

    def test_raises_after_exhausting_retries(self, collector, monkeypatch):
        """거절 응답도 파싱은 되므로, 그냥 두면 '0건 수집'으로 묻힌다."""
        self._serve(monkeypatch, [self._feed(429)] * 3)
        with pytest.raises(RuntimeError, match="429"):
            collector._fetch("u")

    def test_failure_is_isolated_from_other_sources(self, collector, monkeypatch):
        self._serve(monkeypatch, [self._feed(429)] * 3)
        assert collector.safe_collect() == []

    def test_retries_on_server_errors(self, collector, monkeypatch):
        self._serve(monkeypatch, [self._feed(503), self._feed(200, [1])])
        assert len(collector._fetch("u").entries) == 1

    def test_does_not_retry_other_statuses(self, collector, monkeypatch):
        seen = self._serve(monkeypatch, [self._feed(404)])
        assert collector._fetch("u").status == 404
        assert len(seen) == 1

    def test_passes_the_configured_user_agent(self, monkeypatch):
        seen = self._serve(monkeypatch, [self._feed(200, [1])])
        RSSCollector("T", {"url": "u", "user_agent": "UA"})._fetch("u")
        assert seen == ["UA"]


class TestRetryAfter:
    def test_prefers_the_header_when_longer(self):
        assert _retry_after({"retry-after": "30"}, 5) == 30

    def test_keeps_the_floor_when_header_is_shorter(self):
        assert _retry_after({"retry-after": "2"}, 5) == 5

    def test_falls_back_without_a_header(self):
        assert _retry_after({}, 5) == 5
        assert _retry_after({"retry-after": "soon"}, 5) == 5


class TestRegistry:
    def test_type_names_are_registered(self):
        assert {"rss", "reddit"} <= set(Collector.registered_types())

    def test_unknown_type_names_the_options(self):
        with pytest.raises(ValueError, match="알 수 없는 수집기 타입"):
            Collector.create("T", "carrier-pigeon")

    def test_build_skips_disabled_sources(self):
        sources = [
            SourceConfig(name="on", type="rss", options={"url": "u"}),
            SourceConfig(name="off", type="rss", enabled=False, options={"url": "u"}),
        ]
        assert [c.name for c in build_collectors(sources)] == ["on"]


class TestRedditMapping:
    """네트워크 없이 응답 변환만 확인한다."""

    @staticmethod
    def _post(**overrides):
        post = {
            "id": "abc",
            "title": "High score post",
            "score": 500,
            "selftext": "body",
            "permalink": "/r/Test/comments/abc/x/",
            "created_utc": 1755500000,
            "author": "u1",
            "num_comments": 12,
            "subreddit": "Test",
            "stickied": False,
        }
        post.update(overrides)
        return {"data": post}

    def test_filters_by_score_and_stickied(self):
        from core.collectors.reddit import RedditCollector

        collector = RedditCollector("r/Test", {"subreddit": "Test"})
        posts = [
            self._post(),
            self._post(id="low", score=3),
            self._post(id="pinned", stickied=True),
        ]
        kept = list(collector._filter(posts, min_score=100, skip_stickied=True))
        assert [item.external_id for item in kept] == ["abc"]

    def test_builds_permalink_and_metadata(self):
        from core.collectors.reddit import RedditCollector

        collector = RedditCollector("r/Test", {"subreddit": "Test"})
        item = next(iter(collector._filter([self._post()], 0, True)))
        assert item.url == "https://www.reddit.com/r/Test/comments/abc/x/"
        assert item.extra["score"] == 500
        assert item.published_at.tzinfo is timezone.utc
