"""다이제스트 대기열과 발송 시각 판정.

여기 있는 두 클래스는 실제로 메일이 안 나갔던 두 사고를 그대로 고정한 것이다.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.digest import DigestQueue, is_digest_due
from core.notifier import DigestNotifier, Notifier

UTC = timezone.utc
HOUR = 23  # 23:00 UTC = 08:00 KST


class FakeChannel(Notifier):
    name = "email"

    def __init__(self, working: bool = True) -> None:
        self.working = working
        self.batches: list[int] = []

    def send(self, item):
        return self.working

    def send_many(self, items):
        self.batches.append(len(items))
        return len(items) if self.working else 0


class TestQueue:
    def test_roundtrip_preserves_datetime_and_enum(self, tmp_path, processed):
        path = tmp_path / "q.json"
        queue = DigestQueue(path).load()
        assert queue.extend([processed(1), processed(2)]) == 2
        queue.save()

        reloaded = DigestQueue(path).load()
        assert len(reloaded) == 2
        item = reloaded.items()[0]
        assert item.raw.published_at == datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
        assert item.insight.category.value == "LLM"
        assert item.provider == "gemini"

    def test_ignores_items_already_queued(self, tmp_path, processed):
        queue = DigestQueue(tmp_path / "q.json").load()
        queue.extend([processed(1)])
        assert queue.extend([processed(1)]) == 0
        assert len(queue) == 1

    def test_prune_keeps_the_newest(self, tmp_path, processed):
        queue = DigestQueue(tmp_path / "q.json", max_entries=2).load()
        queue.extend([processed(1), processed(2), processed(3)])
        assert [item.raw.external_id for item in queue.items()] == ["id2", "id3"]

    def test_broken_file_does_not_stop_the_run(self, tmp_path):
        path = tmp_path / "q.json"
        path.write_text("{not json", encoding="utf-8")
        assert len(DigestQueue(path).load()) == 0


class TestSendBoundary:
    """22일 아침에 메일이 빠졌던 건. 원인은 UTC 날짜 비교였다."""

    def test_manual_send_earlier_in_the_day_still_allows_the_scheduled_one(self):
        manual = datetime(2026, 8, 21, 3, 31, tzinfo=UTC)
        scheduled = datetime(2026, 8, 21, 23, 12, tzinfo=UTC)
        assert is_digest_due(scheduled, HOUR, manual)

    @pytest.mark.parametrize(
        "now",
        [
            datetime(2026, 8, 21, 23, 45, tzinfo=UTC),  # 같은 시각대 재실행
            datetime(2026, 8, 22, 0, 30, tzinfo=UTC),  # 자정을 넘긴 지연 실행
        ],
    )
    def test_does_not_send_twice_in_one_cycle(self, now):
        sent = datetime(2026, 8, 21, 23, 12, tzinfo=UTC)
        assert not is_digest_due(now, HOUR, sent)

    def test_sends_again_next_cycle(self):
        sent = datetime(2026, 8, 21, 23, 12, tzinfo=UTC)
        assert is_digest_due(datetime(2026, 8, 22, 23, 0, tzinfo=UTC), HOUR, sent)

    def test_holds_before_the_hour(self):
        assert not is_digest_due(datetime(2026, 8, 22, 22, 59, tzinfo=UTC), HOUR, None)

    def test_sends_when_never_sent(self):
        assert is_digest_due(datetime(2026, 8, 22, 23, 0, tzinfo=UTC), HOUR, None)

    def test_treats_naive_timestamps_as_utc(self):
        sent = datetime(2026, 8, 21, 23, 12)
        assert not is_digest_due(datetime(2026, 8, 21, 23, 45, tzinfo=UTC), HOUR, sent)


class TestLateRun:
    """9월 초 사고. 스케줄러가 밀려 23시 창을 놓치자 메일이 6일간 끊겼다.

    발송 조건이 "지금이 23시대인가"였던 탓에, 23:00 슬롯이 자정을 넘겨
    도착하면 그날 분이 통째로 사라졌다. 기준은 시각이 아니라 "직전 23:00
    경계를 지났고 그 뒤로 아직 안 보냈는가"여야 한다.
    """

    def test_run_delayed_past_midnight_still_sends(self):
        sent = datetime(2026, 9, 1, 23, 10, tzinfo=UTC)
        now = datetime(2026, 9, 3, 0, 47, tzinfo=UTC)  # 9/2 23:00 슬롯이 107분 지각
        assert is_digest_due(now, HOUR, sent)

    def test_dropped_slot_is_caught_up_by_the_next_run(self):
        """23:00 슬롯이 통째로 사라져도 밀린 분은 다음 실행이 보낸다."""
        sent = datetime(2026, 8, 28, 23, 16, tzinfo=UTC)
        now = datetime(2026, 8, 30, 6, 31, tzinfo=UTC)  # 8/29 경계는 실행 자체가 없었다
        assert is_digest_due(now, HOUR, sent)

    def test_ordinary_daytime_run_does_not_send(self):
        """지각분을 살리는 것과 아무 때나 보내는 것은 다르다."""
        sent = datetime(2026, 8, 28, 23, 16, tzinfo=UTC)
        now = datetime(2026, 8, 29, 6, 31, tzinfo=UTC)  # 05:00 슬롯이 91분 지각한 것뿐
        assert not is_digest_due(now, HOUR, sent)

    def test_first_ever_run_still_waits_for_the_hour(self):
        """지각분을 살리려다 최초 실행까지 즉시 발송되면 안 된다."""
        assert not is_digest_due(datetime(2026, 9, 3, 0, 47, tzinfo=UTC), HOUR, None)


class TestDigestNotifier:
    """21일 아침에 메일이 빠졌던 건. 이번 실행 해당분이 0건이면 큐째로 건너뛰었다."""

    def test_flushes_backlog_even_when_this_run_adds_nothing(self, tmp_path, processed):
        path = tmp_path / "q.json"
        queue = DigestQueue(path).load()
        queue.extend([processed(i) for i in range(26)])
        queue.save()

        channel = FakeChannel()
        notifier = DigestNotifier(channel, DigestQueue(path).load(), due=True)
        assert notifier.send_many([]) == 26
        assert channel.batches == [26]
        assert len(DigestQueue(path).load()) == 0

    def test_queues_when_not_due(self, tmp_path, processed):
        path = tmp_path / "q.json"
        channel = FakeChannel()
        notifier = DigestNotifier(channel, DigestQueue(path).load(), due=False)

        assert notifier.send_many([processed(1), processed(2)]) == 0
        assert channel.batches == []
        assert len(DigestQueue(path).load()) == 2

    def test_keeps_the_queue_when_sending_fails(self, tmp_path, processed):
        path = tmp_path / "q.json"
        notifier = DigestNotifier(
            FakeChannel(working=False), DigestQueue(path).load(), due=True
        )
        assert notifier.send_many([processed(1), processed(2)]) == 0

        queue = DigestQueue(path).load()
        assert len(queue) == 2
        assert queue.last_sent_at is None, "실패했는데 발송 시각이 기록되면 하루를 건너뛴다"

    def test_records_the_send_time_on_success(self, tmp_path, processed):
        path = tmp_path / "q.json"
        now = datetime(2026, 8, 22, 23, 5, tzinfo=UTC)
        notifier = DigestNotifier(
            FakeChannel(), DigestQueue(path).load(), due=True, now=now
        )
        notifier.send_many([processed(1)])
        assert DigestQueue(path).load().last_sent_at == now


class TestReportWording:
    """로그 문구가 실제로 일어난 일과 어긋나지 않아야 한다."""

    def test_flush_names_both_numbers(self, tmp_path, processed):
        path = tmp_path / "q.json"
        queue = DigestQueue(path).load()
        queue.extend([processed(i) for i in range(29)])
        queue.save()

        notifier = DigestNotifier(FakeChannel(), DigestQueue(path).load(), due=True)
        sent = notifier.send_many([])
        assert notifier.report(0, sent) == "대기열 29건 발송 (이번 실행 0건 포함)"

    def test_hold_reports_the_backlog(self, tmp_path, processed):
        notifier = DigestNotifier(
            FakeChannel(), DigestQueue(tmp_path / "q.json").load(), due=False
        )
        sent = notifier.send_many([processed(1), processed(2)])
        assert notifier.report(2, sent) == "2건 적재 → 대기열 2건 (발송 시각까지 보류)"

    def test_failure_says_the_queue_survives(self, tmp_path, processed):
        notifier = DigestNotifier(
            FakeChannel(working=False), DigestQueue(tmp_path / "q.json").load(), due=True
        )
        sent = notifier.send_many([processed(1)])
        assert notifier.report(1, sent) == "발송 실패 → 대기열 1건 유지, 다음 실행에서 재시도"
