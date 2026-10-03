"""Pure logic for real devices that stay offline: recover, then escalate.

A device is offline while its entity is ``unavailable``/``unknown``. After it has been
offline for :data:`OFFLINE_AFTER`, up to :data:`MAX_ATTEMPTS` recovery attempts run,
:data:`ATTEMPT_GAP` apart. When the last attempt has had its gap, the device escalates
once, and the glue raises a task. A device that comes back clears its record.

Devices that may not be cycled (locks, ovens) skip the attempts and escalate directly.

This module imports nothing from Home Assistant. The glue in ``device_offline_sync.py``
enumerates devices, runs the actions, and owns the time source.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

OFFLINE_AFTER = timedelta(minutes=10)
ATTEMPT_GAP = timedelta(minutes=3)
MAX_ATTEMPTS = 3

OFFLINE_STATES = frozenset({"unavailable", "unknown"})
NO_CYCLE_DOMAINS = frozenset({"lock"})
NO_CYCLE_WORDS = ("oven",)

ACTION_ATTEMPT = "attempt"
ACTION_ESCALATE = "escalate"
ACTION_RECOVERED = "recovered"


def is_offline(state: str | None) -> bool:
    return state is None or state in OFFLINE_STATES


def may_cycle(entity_id: str, device_name: str) -> bool:
    if entity_id.split(".", 1)[0] in NO_CYCLE_DOMAINS:
        return False
    text = f"{entity_id} {device_name}".lower()
    return not any(word in text for word in NO_CYCLE_WORDS)


def step(
    records: dict[str, dict[str, Any]],
    offline: dict[str, bool],
    cycleable: dict[str, bool],
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, int]]]:
    """Advance every tracked device one tick.

    *records* maps ``entity_id`` to ``{"since", "attempts", "last", "escalated"}``.
    *offline* maps every enumerated device to whether it is offline now. A record whose
    device is no longer enumerated is dropped silently. Returns the new records and the
    ordered ``(action, entity_id, attempt_number)`` list the glue must perform.
    """
    new: dict[str, dict[str, Any]] = {}
    actions: list[tuple[str, str, int]] = []

    for entity_id, is_off in offline.items():
        rec = records.get(entity_id)
        if not is_off:
            if rec is not None and rec["escalated"]:
                actions.append((ACTION_RECOVERED, entity_id, rec["attempts"]))
            continue
        if rec is None:
            new[entity_id] = {
                "since": now,
                "attempts": 0,
                "last": None,
                "escalated": False,
            }
            continue

        rec = dict(rec)
        new[entity_id] = rec
        if now - rec["since"] < OFFLINE_AFTER or rec["escalated"]:
            continue

        if not cycleable.get(entity_id, False):
            rec["escalated"] = True
            actions.append((ACTION_ESCALATE, entity_id, rec["attempts"]))
            continue

        if rec["attempts"] < MAX_ATTEMPTS:
            if rec["last"] is None or now - rec["last"] >= ATTEMPT_GAP:
                rec["attempts"] += 1
                rec["last"] = now
                actions.append((ACTION_ATTEMPT, entity_id, rec["attempts"]))
        elif now - rec["last"] >= ATTEMPT_GAP:
            rec["escalated"] = True
            actions.append((ACTION_ESCALATE, entity_id, rec["attempts"]))

    return new, actions
