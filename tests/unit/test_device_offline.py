"""Unit tests for the pure offline-device recovery ladder."""

from datetime import UTC, datetime, timedelta

import hk_device_offline as d

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
ENTITY = "sensor.garage_battery"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def run(records, offline, now, cycleable=None):
    cycleable = {ENTITY: True} if cycleable is None else cycleable
    return d.step(records, {ENTITY: offline}, cycleable, now)


def test_offline_device_is_tracked_but_not_acted_on_before_the_grace_period():
    records, actions = run({}, True, at(0))
    assert ENTITY in records
    assert actions == []
    records, actions = run(records, True, at(9.9))
    assert actions == []


def test_recovery_attempts_run_three_minutes_apart_then_escalate():
    records, _ = run({}, True, at(0))
    kinds = []
    for minute in (10, 13, 16, 19):
        records, actions = run(records, True, at(minute))
        kinds.extend((kind, attempt) for kind, _, attempt in actions)
    assert kinds == [
        (d.ACTION_ATTEMPT, 1),
        (d.ACTION_ATTEMPT, 2),
        (d.ACTION_ATTEMPT, 3),
        (d.ACTION_ESCALATE, 3),
    ]


def test_escalation_happens_once():
    records, _ = run({}, True, at(0))
    for minute in (10, 13, 16, 19):
        records, _ = run(records, True, at(minute))
    records, actions = run(records, True, at(40))
    assert actions == []


def test_a_device_that_recovers_before_escalating_produces_no_event():
    records, _ = run({}, True, at(0))
    records, _ = run(records, True, at(10))
    records, actions = run(records, False, at(11))
    assert actions == []
    assert records == {}


def test_a_device_that_recovers_after_escalating_reports_recovered():
    records, _ = run({}, True, at(0))
    for minute in (10, 13, 16, 19):
        records, _ = run(records, True, at(minute))
    records, actions = run(records, False, at(25))
    assert actions == [(d.ACTION_RECOVERED, ENTITY, 3)]
    assert records == {}


def test_a_device_that_may_not_be_cycled_escalates_without_attempts():
    records, _ = run({}, True, at(0), cycleable={ENTITY: False})
    records, actions = run(records, True, at(10), cycleable={ENTITY: False})
    assert actions == [(d.ACTION_ESCALATE, ENTITY, 0)]
    records, actions = run(records, True, at(13), cycleable={ENTITY: False})
    assert actions == []


def test_a_device_that_disappears_is_dropped_silently():
    records, _ = run({}, True, at(0))
    records, actions = d.step(records, {}, {}, at(5))
    assert records == {}
    assert actions == []


def test_locks_are_never_cycled():
    assert d.may_cycle("lock.front_door", "Front Door") is False


def test_ovens_are_never_cycled_by_entity_or_device_name():
    assert d.may_cycle("sensor.kitchen_oven_current_temperature", "Oven") is False
    assert d.may_cycle("sensor.garage_battery", "Kitchen Oven") is False


def test_other_devices_are_cycled():
    assert d.may_cycle("switch.porch_plug", "Porch Plug") is True


def test_is_offline_treats_missing_unavailable_and_unknown_as_offline():
    assert d.is_offline(None)
    assert d.is_offline("unavailable")
    assert d.is_offline("unknown")
    assert not d.is_offline("on")
    assert not d.is_offline("23.5")
