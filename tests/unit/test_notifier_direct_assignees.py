"""``notifier.async_send_direct_assignees`` — the Profile/Notification-free push path.

A task's own ``assignees`` field, plus the ``assignee_targets`` person -> notify-target
mapping, is enough to get a push out with no saved Profile or Notification at all. These
tests exercise the pure wiring end to end: who gets pinged, what "all" expands to, and
that the synthetic notification this path builds still rides the same
``build_notification``/action-button machinery a stored walk Notification uses.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from notifier_harness import FakeCoord, FakeHass, load_notifier, overdue_task

notifier = load_notifier()
notifications = sys.modules["hk.notifications"]

OVERDUE = notifier.EVENT_TASK_OVERDUE
DUE_SOON = notifier.EVENT_TASK_DUE_SOON


def _task(tid: str, *, assignees: list[str], days: int = 3) -> dict[str, Any]:
    return {**overdue_task(tid, days=days), "assignees": assignees}


def _fired(
    *task_ids: str, event: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    event = event or OVERDUE
    return [(event, {"task_id": tid}) for tid in task_ids]


def _run(hass, coord, fired) -> None:
    asyncio.run(notifier.async_send_direct_assignees(hass, coord, fired))


def test_pushes_straight_to_the_assignees_configured_target():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, _fired("t1"))

    assert hass.services.calls == [
        ("notify", "mobile_app_dan", hass.services.calls[0][2])
    ]


def test_each_of_several_assignees_gets_their_own_push():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan", "person.angie"])},
        {
            "assignee_targets": [
                {"person": "person.dan", "targets": ["mobile_app_dan"]},
                {"person": "person.angie", "targets": ["mobile_app_angie"]},
            ]
        },
    )

    _run(hass, coord, _fired("t1"))

    targets_called = sorted(service for _, service, _ in hass.services.calls)
    assert targets_called == ["mobile_app_angie", "mobile_app_dan"]


def test_all_expands_to_every_configured_person():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["all"])},
        {
            "assignee_targets": [
                {"person": "person.dan", "targets": ["mobile_app_dan"]},
                {"person": "person.angie", "targets": ["mobile_app_angie"]},
            ]
        },
    )

    _run(hass, coord, _fired("t1"))

    targets_called = sorted(service for _, service, _ in hass.services.calls)
    assert targets_called == ["mobile_app_angie", "mobile_app_dan"]


def test_a_person_with_more_than_one_phone_gets_both():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])},
        {
            "assignee_targets": [
                {
                    "person": "person.dan",
                    "targets": ["mobile_app_dans_phone", "mobile_app_kitchen_tablet"],
                }
            ]
        },
    )

    _run(hass, coord, _fired("t1"))

    targets_called = sorted(service for _, service, _ in hass.services.calls)
    assert targets_called == ["mobile_app_dans_phone", "mobile_app_kitchen_tablet"]


def test_an_unmapped_assignee_sends_nothing():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.stranger"])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, _fired("t1"))

    assert hass.services.calls == []


def test_an_unassigned_task_sends_nothing():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=[])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, _fired("t1"))

    assert hass.services.calls == []


def test_no_configured_mapping_is_a_no_op():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])}, {"assignee_targets": []}
    )

    _run(hass, coord, _fired("t1"))

    assert hass.services.calls == []


def test_only_reacts_to_overdue_or_due_soon_transitions():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, [("home_keeper_task_completed", {"task_id": "t1"})])

    assert hass.services.calls == []


def test_repeated_fires_for_the_same_task_and_person_replace_in_place():
    """Same ``notification_tag`` each time -> the phone replaces, not stacks."""
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, _fired("t1"))
    _run(hass, coord, _fired("t1"))

    tags = {call[2]["data"]["tag"] for call in hass.services.calls}
    assert tags == {notifications.notification_tag("direct-t1-person.dan")}


def test_the_synthetic_notification_carries_the_normal_action_buttons():
    hass = FakeHass()
    coord = FakeCoord(
        {"t1": _task("t1", assignees=["person.dan"])},
        {"assignee_targets": [{"person": "person.dan", "targets": ["mobile_app_dan"]}]},
    )

    _run(hass, coord, _fired("t1"))

    payload = hass.services.calls[0][2]
    verbs = {a["action"].split("::")[1] for a in payload["data"]["actions"]}
    assert verbs == {"complete", "snooze", "open"}
