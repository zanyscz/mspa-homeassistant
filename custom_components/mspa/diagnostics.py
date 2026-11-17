"""Diagnostics for the MSpa integration — the "Download diagnostics" button.

What a bug report needs is what the cloud actually said, and until now the only way
to get it was to switch on debug logging and fish the payload out of the log. This
hands over the same material in one file: the raw-ish status, the device record, the
detail record and the integration's own view of polling and pending commands.

Everything that identifies the account or the unit is redacted before it leaves.
"""
from __future__ import annotations

from typing import Any

from .const import DOMAIN

REDACTED = "**REDACTED**"

# Keys whose values identify the owner or the individual unit. Matched by name at any
# depth, so a field the cloud adds to a nested record later is still caught if it uses
# one of these names.
TO_REDACT = frozenset({
    "account_email", "email", "password", "token", "authorization",
    "device_id", "device_uuid", "sn", "serial_number", "mac", "mac_address",
    "certificate", "enduser_id", "app_id", "visitor_id", "unique_id", "title",
    "device_alias", "name",
})

# Coordinator attributes worth reporting, read defensively: a coordinator that failed
# part-way through setup may not have all of them.
_COORDINATOR_FIELDS = (
    "series", "model", "software_version", "wifi_version", "mcu_version",
    "product_id", "last_seen_utc", "update_interval",
    "computed_heat_rate", "computed_cool_rate",
    "_pending_changes", "_pending_raw_command", "_command_retry_count",
)


def redact(value: Any) -> Any:
    """Return a copy of `value` with identifying fields replaced, at any depth."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and k.lower() in TO_REDACT and v not in (None, "")
                else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(v) for v in value]
    return value


def _plain(value: Any) -> Any:
    """Make a value JSON-friendly without losing what it said."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)


def build_diagnostics(entry, coordinator) -> dict:
    """Assemble the diagnostics payload. Pure, so it can be tested without HA."""
    state = {
        field.lstrip("_"): _plain(getattr(coordinator, field, None))
        for field in _COORDINATOR_FIELDS
    }
    state["fault_code"] = getattr(coordinator, "fault_code", None)
    return {
        "entry": {
            "data": redact(_plain(dict(getattr(entry, "data", {}) or {}))),
            "options": redact(_plain(dict(getattr(entry, "options", {}) or {}))),
        },
        "coordinator": redact(state),
        "status": redact(_plain(getattr(coordinator, "_last_data", {}) or {})),
        "device_detail": redact(_plain(getattr(coordinator, "device_detail", {}) or {})),
    }


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return build_diagnostics(entry, coordinator)
