"""Home-Assistant-aware glue for real devices that stay offline.

Every minute this enumerates the real device entities (ones that belong to a device and
are not Home Keeper's own, a companion app, or a non-device domain), feeds their
availability to the pure ladder in ``device_offline.py``, and carries out what it
returns: a recovery attempt (reload the integration, or press a reboot button on the
second try), an escalation (a task assigned to :data:`DEVICE_OFFLINE_ASSIGNEE` plus an
event), or a recovery (the task is removed plus an event).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import device_offline, events
from .const import (
    DEVICE_OFFLINE_ASSIGNEE,
    DOMAIN,
    EVENT_DEVICE_OFFLINE,
    EVENT_DEVICE_RECOVERED,
    REC_TRIGGERED,
    TASK_SOURCE_DEVICE_OFFLINE,
)

_LOGGER = logging.getLogger(__name__)

_TICK = timedelta(minutes=1)
_IGNORED_DOMAINS = frozenset(
    {
        "automation",
        "button",
        "calendar",
        "conversation",
        "device_tracker",
        "event",
        "group",
        "image",
        "notify",
        "person",
        "scene",
        "script",
        "sun",
        "todo",
        "tts",
        "update",
        "zone",
    }
)
_IGNORED_PLATFORMS = frozenset({DOMAIN, "mobile_app"})
_REBOOT_WORDS = ("restart", "reboot")
_REBOOT_ATTEMPT = 2


class DeviceOfflineSync:
    """Runs the recover-then-escalate ladder for every real device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, coordinator: Any
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._records: dict[str, dict[str, Any]] = {}

    @property
    def _store(self) -> Any:
        return self._coordinator.store

    def async_start(self) -> None:
        self._entry.async_on_unload(
            async_track_time_interval(self._hass, self._tick, _TICK)
        )

    async def _tick(self, _now: datetime) -> None:
        now = dt_util.now()
        names: dict[str, str] = {}
        device_ids: dict[str, str | None] = {}
        offline: dict[str, bool] = {}
        cycleable: dict[str, bool] = {}
        config_entries: dict[str, str | None] = {}

        ent_reg = er.async_get(self._hass)
        for entry in ent_reg.entities.values():
            if (
                entry.device_id is None
                or entry.disabled
                or entry.domain in _IGNORED_DOMAINS
                or entry.platform in _IGNORED_PLATFORMS
            ):
                continue
            state = self._hass.states.get(entry.entity_id)
            name = self._name(entry, state)
            names[entry.entity_id] = name
            device_ids[entry.entity_id] = entry.device_id
            config_entries[entry.entity_id] = entry.config_entry_id
            offline[entry.entity_id] = device_offline.is_offline(
                state.state if state is not None else None
            )
            cycleable[entry.entity_id] = device_offline.may_cycle(entry.entity_id, name)

        previous = self._records
        records, actions = device_offline.step(previous, offline, cycleable, now)
        self._records = records

        for kind, entity_id, attempt in actions:
            if kind == device_offline.ACTION_ATTEMPT:
                await self._attempt(
                    entity_id, attempt, config_entries[entity_id], names
                )
            elif kind == device_offline.ACTION_ESCALATE:
                await self._escalate(entity_id, attempt, names, device_ids, now)
            elif kind == device_offline.ACTION_RECOVERED:
                since = previous[entity_id]["since"]
                await self._recover(entity_id, attempt, names, device_ids, since, now)

        await self._sweep_orphans(offline)

    def _name(self, entry: er.RegistryEntry, state: Any) -> str:
        if state is not None and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return entry.name or entry.original_name or entry.entity_id

    async def _attempt(
        self,
        entity_id: str,
        attempt: int,
        config_entry_id: str | None,
        names: dict[str, str],
    ) -> None:
        _LOGGER.info("Recovery attempt %d for offline device %s", attempt, entity_id)
        if attempt == _REBOOT_ATTEMPT:
            button = self._reboot_button(entity_id)
            if button is not None:
                try:
                    await self._hass.services.async_call(
                        "button", "press", {"entity_id": button}, blocking=True
                    )
                except HomeAssistantError as err:
                    _LOGGER.debug("Reboot button %s failed: %s", button, err)
                return
        if config_entry_id is None:
            return
        try:
            await self._hass.config_entries.async_reload(config_entry_id)
        except HomeAssistantError as err:
            _LOGGER.debug(
                "Reload of %s for %s failed: %s", config_entry_id, entity_id, err
            )

    def _reboot_button(self, entity_id: str) -> str | None:
        ent_reg = er.async_get(self._hass)
        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.device_id is None:
            return None
        for candidate in er.async_entries_for_device(ent_reg, entry.device_id):
            if candidate.domain == "button" and any(
                word in candidate.entity_id for word in _REBOOT_WORDS
            ):
                return candidate.entity_id
        return None

    async def _escalate(
        self,
        entity_id: str,
        attempts: int,
        names: dict[str, str],
        device_ids: dict[str, str | None],
        now: datetime,
    ) -> None:
        name = names.get(entity_id, entity_id)
        device_id = device_ids.get(entity_id)
        since = self._records.get(entity_id, {}).get("since", now)
        _LOGGER.warning("Device %s is still offline after %d attempts", name, attempts)
        if self._open_task(entity_id) is None:
            task = await self._store.add_task(
                {
                    "name": f"Device offline: {name}",
                    "recurrence_type": REC_TRIGGERED,
                    "device_id": device_id,
                    "notes": "Home Keeper could not bring this device back online.",
                    "assignees": [DEVICE_OFFLINE_ASSIGNEE],
                    "source": {TASK_SOURCE_DEVICE_OFFLINE: {"entity_id": entity_id}},
                }
            )
            await self._store.trigger_task(task["id"])
        self._hass.bus.async_fire(
            EVENT_DEVICE_OFFLINE,
            events.device_offline_event_data(
                {
                    "entity_id": entity_id,
                    "name": name,
                    "device_id": device_id,
                    "attempts": attempts,
                    "offline_since": since.isoformat(),
                }
            ),
        )

    async def _recover(
        self,
        entity_id: str,
        attempts: int,
        names: dict[str, str],
        device_ids: dict[str, str | None],
        since: datetime,
        now: datetime,
    ) -> None:
        name = names.get(entity_id, entity_id)
        device_id = device_ids.get(entity_id)
        task = self._open_task(entity_id)
        if task is not None:
            await self._store.delete_task(task["id"], force=True)
        _LOGGER.info("Device %s is back online", name)
        self._hass.bus.async_fire(
            EVENT_DEVICE_RECOVERED,
            events.device_recovered_event_data(
                {
                    "entity_id": entity_id,
                    "name": name,
                    "device_id": device_id,
                    "attempts": attempts,
                    "offline_since": since.isoformat(),
                    "recovered_at": now.isoformat(),
                }
            ),
        )

    async def _sweep_orphans(self, offline: dict[str, bool]) -> None:
        """Drop an offline task whose device is online and no longer tracked."""
        for task in self._offline_tasks():
            entity_id = self._task_entity(task)
            if entity_id is None or entity_id in self._records:
                continue
            if not offline.get(entity_id, False):
                await self._store.delete_task(task["id"], force=True)

    def _open_task(self, entity_id: str) -> dict[str, Any] | None:
        for task in self._offline_tasks():
            if self._task_entity(task) == entity_id:
                return task
        return None

    def _offline_tasks(self) -> list[dict[str, Any]]:
        return [
            t
            for t in self._store.get_tasks().values()
            if self._task_entity(t) is not None
        ]

    @staticmethod
    def _task_entity(task: dict[str, Any]) -> str | None:
        source = task.get("source")
        if not isinstance(source, dict):
            return None
        info = source.get(TASK_SOURCE_DEVICE_OFFLINE)
        if not isinstance(info, dict):
            return None
        entity_id = info.get("entity_id")
        return entity_id if isinstance(entity_id, str) else None
