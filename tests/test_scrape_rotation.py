"""Tests for the daily scrape-time rotation (app.scrape_rotation) and the CI
monitoring enqueue gate that uses it."""

from datetime import date

from app import enqueue_monitoring, scrape_rotation


def test_rotation_cycles_morning_afternoon_night():
    # Consecutive days step through the three windows in order.
    d0 = date(2026, 1, 1)
    seen = [scrape_rotation.active_slot(date.fromordinal(d0.toordinal() + i)) for i in range(6)]
    # Whatever the phase, it's a repeating 3-cycle with all three present.
    assert set(seen) == set(scrape_rotation.SLOTS)
    assert seen[0] == seen[3] and seen[1] == seen[4] and seen[2] == seen[5]


def test_is_active_matches_exactly_one_slot_per_day():
    day = date(2026, 3, 10)
    actives = [s for s in scrape_rotation.SLOTS if scrape_rotation.is_active(s, day)]
    assert len(actives) == 1
    assert actives[0] == scrape_rotation.active_slot(day)


def test_enqueue_monitoring_runs_only_todays_slot(app, monkeypatch):
    # main() runs enqueue_all only for the slot the rotation picks today; the other
    # two invocations no-op (the cost cut from 3x/day to 1x/day).
    with app.app_context():
        monkeypatch.setattr(enqueue_monitoring, "load_dotenv", lambda: None)
        monkeypatch.setattr(enqueue_monitoring, "_connect",
                            lambda: (_ for _ in ()).throw(AssertionError("connected on a no-op slot")))
        calls = []
        # Patch enqueue_all + _connect only for the active-slot path.
        today_slot = scrape_rotation.active_slot()

        # Non-active slots: must return 0 WITHOUT connecting (the _connect above
        # raises if reached).
        for slot in scrape_rotation.SLOTS:
            if slot != today_slot:
                assert enqueue_monitoring.main([slot]) == 0

        # Active slot: connects + enqueues. Re-patch _connect to a no-op conn and
        # enqueue_all to record the call.
        monkeypatch.setattr(enqueue_monitoring, "_connect",
                            lambda: _FakeConn())
        monkeypatch.setattr(enqueue_monitoring, "enqueue_all",
                            lambda conn, slot: calls.append(slot) or 0)
        assert enqueue_monitoring.main([today_slot]) == 0
        assert calls == [today_slot]


class _FakeConn:
    def close(self):
        pass
