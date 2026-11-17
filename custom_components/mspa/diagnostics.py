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
    # The detail record's own additions: the address the unit was activated from, and
    # the cloud's storage keys, which are the device id with a prefix.
    "activate_ip", "ip", "pk", "sk", "rl_sort", "ct_sort",
})

# Coordinator attributes worth reporting, read defensively: a coordinator that failed
# part-way through setup may not have all of them.
_COORDINATOR_FIELDS = (
    "series", "model", "software_version", "wifi_version", "mcu_version",
    "product_id", "last_seen_utc", "update_interval",
    "computed_heat_rate", "computed_cool_rate",
    "_pending_changes", "_pending_raw_command", "_command_retry_count",
)


# Redacted where they stand, but not hunted down elsewhere. An unnamed spa's alias is
# its model number, and scrubbing every occurrence of that would remove the one field a
# bug report most needs.
_LABELS = frozenset({"name", "title", "device_alias"})


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


def _secrets(value: Any, found: set | None = None) -> set:
    """Every identifying value in `value`, so it can be scrubbed where it is embedded.

    Redacting by key name is not enough on its own. The cloud repeats the device id
    inside other fields — its storage keys are `d_<device id>` — and a key the cloud
    adds next year will not be on the list. Short values are skipped: scrubbing every
    occurrence of a two-character string would shred the payload.
    """
    found = set() if found is None else found
    if isinstance(value, dict):
        for k, v in value.items():
            if (isinstance(k, str) and k.lower() in TO_REDACT
                    and k.lower() not in _LABELS
                    and isinstance(v, str) and len(v) >= 6):
                found.add(v)
            else:
                _secrets(v, found)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            _secrets(v, found)
    return found


def _scrub(value: Any, secrets: set) -> Any:
    """Replace any secret embedded in a string, at any depth."""
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
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
    raw = {
        "entry": {
            "data": _plain(dict(getattr(entry, "data", {}) or {})),
            "options": _plain(dict(getattr(entry, "options", {}) or {})),
        },
        "coordinator": state,
        "status": _plain(getattr(coordinator, "_last_data", {}) or {}),
        "device_detail": _plain(getattr(coordinator, "device_detail", {}) or {}),
    }
    # Collected before redaction, while the values are still there to be collected.
    secrets = _secrets(raw)
    for field in ("device_id", "serial_number", "mac_address"):
        value = getattr(coordinator, field, None)
        if isinstance(value, str) and len(value) >= 6:
            secrets.add(value)
    return _scrub(redact(raw), secrets)


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return build_diagnostics(entry, coordinator)
