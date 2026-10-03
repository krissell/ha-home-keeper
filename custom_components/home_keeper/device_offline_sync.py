"""Home-Assistant-aware glue for real devices that stay offline.

A *device* is offline when every one of its enabled entities is ``unavailable`` or
``unknown``. Judging by device, not entity, keeps a camera's always-unavailable AI
counters from reading as a dead device while the camera itself is online.

Every minute this feeds each device's availability to the pure ladder in
``device_offline.py`` and carries out what it returns: a recovery attempt (reload the
device's integration, or press a reboot button on the second try), an escalation (a
task assigned to :data:`DEVICE_OFFLINE_ASSIGNEE` plus an event), or a recovery (the
task is removed plus an event).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
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
        ent_reg = er.async_get(self._hass)
        dev_reg = dr.async_get(self._hass)

        by_device: dict[str, list[er.RegistryEntry]] = {}
        for entry in ent_reg.entities.values():
            if (
                entry.device_id is None
                or entry.disabled
                or entry.domain in _IGNORED_DOMAINS
                or entry.platform in _IGNORED_PLATFORMS
            ):
                continue
            by_device.setdefault(entry.device_id, []).append(entry)

        names: dict[str, str] = {}
        config_entries: dict[str, str | None] = {}
        offline: dict[str, bool] = {}
        cycleable: dict[str, bool] = {}
        for device_id, entries in by_device.items():
            device = dev_reg.async_get(device_id)
            name = (device.name_by_user or device.name) if device else None
            name = name or entries[0].entity_id
            names[device_id] = name
            config_entries[device_id] = device.primary_config_entry if device else None
            offline[device_id] = all(
                device_offline.is_offline(self._state(e.entity_id)) for e in entries
            )
            cycleable[device_id] = all(
                device_offline.may_cycle(e.entity_id, name) for e in entries
            )

        previous = self._records
        records, actions = device_offline.step(previous, offline, cycleable, now)
        self._records = records

        for kind, device_id, attempt in actions:
            if kind == device_offline.ACTION_ATTEMPT:
                await self._attempt(device_id, attempt, config_entries.get(device_id))
            elif kind == device_offline.ACTION_ESCALATE:
                await self._escalate(device_id, attempt, names, now)
            elif kind == device_offline.ACTION_RECOVERED:
                since = previous[device_id]["since"]
                await self._recover(device_id, attempt, names, since, now)

        await self._sweep_orphans(offline)

    def _state(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)
        return state.state if state is not None else None

    async def _attempt(
        self, device_id: str, attempt: int, config_entry_id: str | None
    ) -> None:
        _LOGGER.info("Recovery attempt %d for offline device %s", attempt, device_id)
        if attempt == _REBOOT_ATTEMPT:
            button = self._reboot_button(device_id)
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
                "Reload of %s for %s failed: %s", config_entry_id, device_id, err
            )

    def _reboot_button(self, device_id: str) -> str | None:
        ent_reg = er.async_get(self._hass)
        for candidate in er.async_entries_for_device(ent_reg, device_id):
            if candidate.domain == "button" and any(
                word in candidate.entity_id for word in _REBOOT_WORDS
            ):
                return candidate.entity_id
        return None

    async def _escalate(
        self, device_id: str, attempts: int, names: dict[str, str], now: datetime
    ) -> None:
        name = names.get(device_id, device_id)
        since = self._records.get(device_id, {}).get("since", now)
        _LOGGER.warning("Device %s is still offline after %d attempts", name, attempts)
        if self._open_task(device_id) is None:
            task = await self._store.add_task(
                {
                    "name": f"Device offline: {name}",
                    "recurrence_type": REC_TRIGGERED,
                    "device_id": device_id,
                    "notes": "Home Keeper could not bring this device back online.",
                    "assignees": [DEVICE_OFFLINE_ASSIGNEE],
                    "source": {TASK_SOURCE_DEVICE_OFFLINE: {"device_id": device_id}},
                }
            )
            await self._store.trigger_task(task["id"])
        self._hass.bus.async_fire(
            EVENT_DEVICE_OFFLINE,
            events.device_offline_event_data(
                {
                    "device_id": device_id,
                    "name": name,
                    "attempts": attempts,
                    "offline_since": since.isoformat(),
                }
            ),
        )

    async def _recover(
        self,
        device_id: str,
        attempts: int,
        names: dict[str, str],
        since: datetime,
        now: datetime,
    ) -> None:
        name = names.get(device_id, device_id)
        task = self._open_task(device_id)
        if task is not None:
            await self._store.delete_task(task["id"], force=True)
        _LOGGER.info("Device %s is back online", name)
        self._hass.bus.async_fire(
            EVENT_DEVICE_RECOVERED,
            events.device_recovered_event_data(
                {
                    "device_id": device_id,
                    "name": name,
                    "attempts": attempts,
                    "offline_since": since.isoformat(),
                    "recovered_at": now.isoformat(),
                }
            ),
        )

    async def _sweep_orphans(self, offline: dict[str, bool]) -> None:
        """Drop an offline task whose device is online and no longer tracked."""
        for task in self._offline_tasks():
            device_id = self._task_device(task)
            if device_id is None or device_id in self._records:
                continue
            if not offline.get(device_id, False):
                await self._store.delete_task(task["id"], force=True)

    def _open_task(self, device_id: str) -> dict[str, Any] | None:
        for task in self._offline_tasks():
            if self._task_device(task) == device_id:
                return task
        return None

    def _offline_tasks(self) -> list[dict[str, Any]]:
        return [
            t
            for t in self._store.get_tasks().values()
            if self._task_device(t) is not None
        ]

    @staticmethod
    def _task_device(task: dict[str, Any]) -> str | None:
        source = task.get("source")
        if not isinstance(source, dict):
            return None
        info = source.get(TASK_SOURCE_DEVICE_OFFLINE)
        if not isinstance(info, dict):
            return None
        device_id = info.get("device_id")
        return device_id if isinstance(device_id, str) else None
