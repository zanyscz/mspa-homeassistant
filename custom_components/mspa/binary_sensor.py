"""Binary sensor platform for MSpa — is the spa still answering?

Every other entity this integration provides goes `unavailable` the moment the spa
stops answering, which is precisely when something needs to say so. An automation
cannot trigger on the absence of a state, and `unavailable` is indistinguishable
from a restart, a reload, or Home Assistant itself starting up.

So this entity never goes unavailable. It reports connectivity as a state in its
own right, and it waits out the short outages that are not worth waking anyone for.
"""
import logging
from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_OFFLINE_AFTER_MINUTES,
    DEFAULT_OFFLINE_AFTER_MINUTES,
)
from .entity import MSpaBinarySensorEntity

_LOGGER = logging.getLogger(__name__)


def connectivity(last_seen_utc, now_utc, grace_seconds):
    """Resolve a last-seen time into (is_on, offline_seconds).

    `is_on` follows the CONNECTIVITY device class: True is connected. None means
    the spa has not been seen even once, which is not the same as being offline —
    a fresh install with bad credentials has never seen it either, and saying
    "disconnected" there would be a guess.

    The grace window is the whole reason this is a function rather than a
    comparison. Measured on this installation: the daily Starlink outage drops
    every entity for about 30 seconds, and on 07.10.2026 the vendor cloud dropped
    23 single polls in ten hours while the spa itself stayed on the wifi
    throughout. Neither is an outage anyone wants waking them, and both are
    minutes short of the real one that followed.
    """
    if last_seen_utc is None:
        return None, None
    offline = (now_utc - last_seen_utc).total_seconds()
    if offline < 0:
        offline = 0.0
    return offline <= grace_seconds, offline


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [MSpaConnectivity(coordinator), MSpaProblem(coordinator)]
    # The filter warning arrives in the status payload on firmware that has it.
    if "warning" in coordinator._last_data:
        entities.append(MSpaFilterChangeDue(coordinator))
    async_add_entities(entities)


class MSpaConnectivity(MSpaBinarySensorEntity):
    """Whether the spa has answered recently enough to call it connected.

    Pair it with a `for:` of your own on top of the grace window if you want to be
    even more patient. The attributes carry the raw material either way: when it
    was last seen, and how long it has been gone.
    """

    name = "Connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info
        self._attr_unique_id = (
            f"mspa_connectivity_{getattr(coordinator, 'device_id', 'unknown')}"
        )

    @property
    def available(self) -> bool:
        """Always. An entity that reports outages cannot opt out of existing during one.

        This deliberately does not call `super().available`, which is False whenever
        the coordinator's last update failed — the exact condition this entity is
        here to describe.
        """
        return True

    @property
    def _grace_seconds(self) -> float:
        entry = getattr(self.coordinator, "config_entry", None)
        options = getattr(entry, "options", None) or {}
        try:
            minutes = float(options.get(
                CONF_OFFLINE_AFTER_MINUTES, DEFAULT_OFFLINE_AFTER_MINUTES))
        except (TypeError, ValueError):
            minutes = DEFAULT_OFFLINE_AFTER_MINUTES
        if minutes <= 0:
            minutes = DEFAULT_OFFLINE_AFTER_MINUTES
        return minutes * 60.0

    @property
    def is_on(self):
        state, _ = connectivity(
            getattr(self.coordinator, "last_seen_utc", None),
            dt_util.utcnow(),
            self._grace_seconds,
        )
        return state

    @property
    def extra_state_attributes(self):
        last_seen = getattr(self.coordinator, "last_seen_utc", None)
        _, offline = connectivity(last_seen, dt_util.utcnow(), self._grace_seconds)
        return {
            "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime) else None,
            "offline_minutes": round(offline / 60.0, 1) if offline is not None else None,
            "grace_minutes": round(self._grace_seconds / 60.0, 1),
        }


# What the spa shows on its panel for each fault, as far as the owner's manuals say.
# Only codes with a documented meaning are listed; anything else is reported by code
# alone rather than given a guessed description.
FAULT_DESCRIPTIONS = {
    "F1": "Water flow or temperature sensor fault. Check the water level, clean the "
          "filter and the pipes, then power-cycle the control box.",
}

# The one warning code whose meaning is established: the panel's filter indicator.
WARNING_FILTER_CHANGE = "A0"


class MSpaProblem(MSpaBinarySensorEntity):
    """On while the spa reports a fault code.

    The Fault sensor already carries the code, but a code is awkward to trigger on:
    an automation has to know that "OK" is the healthy value and that everything
    else is not. This is the same information as a plain problem / no problem, which
    is what a notification or a dashboard badge wants.
    """

    name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info
        self._attr_unique_id = (
            f"mspa_problem_{getattr(coordinator, 'device_id', 'unknown')}")

    @property
    def is_on(self):
        return self.coordinator.fault_code is not None

    @property
    def extra_state_attributes(self):
        code = self.coordinator.fault_code
        return {
            "fault_code": code,
            "description": FAULT_DESCRIPTIONS.get(code) if code else None,
        }


class MSpaFilterChangeDue(MSpaBinarySensorEntity):
    """On while the spa is asking for its filter cartridge to be changed.

    Mirrors warning code A0, the filter indicator on the panel. Disabled by default
    for the same reason the Filter status sensor is: on some units the flag stays set
    after the reminder has been reset at the panel, so it is offered rather than
    imposed. The warning code is in the attributes for anyone comparing the two.
    """

    name = "Filter change due"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_device_info = self.device_info
        self._attr_unique_id = (
            f"mspa_filter_change_due_{getattr(coordinator, 'device_id', 'unknown')}")

    @property
    def is_on(self):
        warning = self.coordinator._last_data.get("warning")
        if warning is None:
            return None
        return warning == WARNING_FILTER_CHANGE

    @property
    def extra_state_attributes(self):
        return {"warning_code": self.coordinator._last_data.get("warning") or None}
