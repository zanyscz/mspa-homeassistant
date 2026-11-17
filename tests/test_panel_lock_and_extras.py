"""Tests for the panel lock, the problem sensors, the heating state, the finer power
model, diagnostics, the Czech translation and the climate entity's two API shapes.

Run with: python -m pytest tests/test_panel_lock_and_extras.py -v
"""
import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.mspa import climate as climate_mod
from custom_components.mspa.binary_sensor import (
    MSpaFilterChangeDue,
    MSpaProblem,
)
from custom_components.mspa.const import (
    CONF_BUBBLE_POWER_L1,
    CONF_JET_POWER,
    DEFAULT_BUBBLE_POWER,
    DEFAULT_BUBBLE_POWER_L1,
    DEFAULT_BUBBLE_POWER_L2,
    DEFAULT_JET_POWER,
)
from custom_components.mspa.coordinator import MSpaUpdateCoordinator
from custom_components.mspa.diagnostics import REDACTED, build_diagnostics, redact
from custom_components.mspa.sensor import (
    MSpaHeatingStateSensor,
    _calculate_total_power,
)
from custom_components.mspa.switch import MSpaPanelLockSwitch
from homeassistant.exceptions import HomeAssistantError

_PKG = Path(__file__).parent.parent / "custom_components" / "mspa"


def _run(coro):
    return asyncio.run(coro)


def _entity(cls, **last_data):
    """An entity built without HA, reading from a coordinator that holds `last_data`."""
    coord = MagicMock()
    coord._last_data = dict(last_data)
    fault = last_data.get("fault")
    coord.fault_code = None if fault in (None, "", "OK") else str(fault)
    e = object.__new__(cls)
    e.coordinator = coord
    return e


def _coord(**last_data) -> MSpaUpdateCoordinator:
    c = object.__new__(MSpaUpdateCoordinator)
    c._pending_changes = {}
    c._pending_raw_command = {}
    c._command_retry_count = 0
    c._rapid_poll_until = None
    c.update_interval = MagicMock()
    c._last_data = dict(last_data)
    c.api = MagicMock()
    c.api.set_safety_lock = AsyncMock(return_value={"message": "SUCCESS"})
    c.async_request_refresh = AsyncMock()
    return c


def _entry(options=None):
    e = MagicMock()
    e.options = options or {}
    return e


class TestPanelLockCommand:
    def test_lock_sends_one_and_waits_for_it(self):
        c = _coord(safety_lock=0)
        _run(c.set_panel_lock(True))
        c.api.set_safety_lock.assert_awaited_once_with(1)
        assert c._pending_changes == {"safety_lock": 1}
        assert c._pending_raw_command == {"safety_lock": 1}
        c.async_request_refresh.assert_awaited_once()

    def test_unlock_sends_zero(self):
        c = _coord(safety_lock=1)
        _run(c.set_panel_lock(False))
        c.api.set_safety_lock.assert_awaited_once_with(0)
        assert c._pending_changes == {"safety_lock": 0}

    def test_a_refused_command_is_an_error_and_arms_nothing(self):
        c = _coord(safety_lock=0)
        c.api.set_safety_lock = AsyncMock(return_value={"message": "FAIL", "code": 1})
        with pytest.raises(HomeAssistantError):
            _run(c.set_panel_lock(True))
        assert c._pending_changes == {}
        c.async_request_refresh.assert_not_awaited()

    def test_a_network_error_becomes_a_readable_one(self):
        c = _coord(safety_lock=0)
        c.api.set_safety_lock = AsyncMock(side_effect=OSError("no route"))
        with pytest.raises(HomeAssistantError):
            _run(c.set_panel_lock(True))
        assert c._pending_changes == {}

    def test_it_does_not_hand_a_waiting_command_another_retry(self):
        c = _coord(safety_lock=0)
        c._pending_changes = {"heater": "on"}
        c._pending_raw_command = {"heater_state": 1}
        c._command_retry_count = 1
        _run(c.set_panel_lock(True))
        assert c._command_retry_count == 1
        assert c._pending_changes == {"heater": "on", "safety_lock": 1}

    def test_a_fault_does_not_block_the_lock(self):
        """Locking the panel of a faulting spa is a reasonable thing to want."""
        c = _coord(safety_lock=0, fault="F1")
        _run(c.set_panel_lock(True))
        c.api.set_safety_lock.assert_awaited_once_with(1)


class TestPanelLockSwitch:
    @pytest.mark.parametrize("raw, expected", [(1, True), (0, False), (None, None)])
    def test_state_follows_the_payload(self, raw, expected):
        data = {} if raw is None else {"safety_lock": raw}
        assert _entity(MSpaPanelLockSwitch, **data).is_on is expected

    def test_stays_operable_during_a_fault(self):
        assert MSpaPanelLockSwitch._unavailable_while_faulting is False

    def test_turning_on_and_off_calls_the_coordinator(self):
        s = _entity(MSpaPanelLockSwitch, safety_lock=0)
        s.coordinator.set_panel_lock = AsyncMock()
        _run(s.async_turn_on())
        _run(s.async_turn_off())
        assert [c.args for c in s.coordinator.set_panel_lock.await_args_list] == [
            (True,), (False,)]


class TestProblemSensors:
    def test_no_fault_is_no_problem(self):
        e = _entity(MSpaProblem, fault="OK")
        assert e.is_on is False
        assert e.extra_state_attributes == {"fault_code": None, "description": None}

    def test_a_fault_is_a_problem_and_names_the_code(self):
        e = _entity(MSpaProblem, fault="F1")
        assert e.is_on is True
        attrs = e.extra_state_attributes
        assert attrs["fault_code"] == "F1"
        assert attrs["description"]

    def test_an_undocumented_code_gets_no_invented_description(self):
        e = _entity(MSpaProblem, fault="E9")
        assert e.is_on is True
        assert e.extra_state_attributes["description"] is None

    @pytest.mark.parametrize("warning, expected", [("A0", True), ("", False), ("00", False)])
    def test_filter_change_follows_a0(self, warning, expected):
        assert _entity(MSpaFilterChangeDue, warning=warning).is_on is expected

    def test_filter_change_is_unknown_without_the_key(self):
        assert _entity(MSpaFilterChangeDue).is_on is None


class TestHeatingState:
    @pytest.mark.parametrize("raw, expected", [
        (0, "off"), (2, "preheating"), (3, "heating"), (4, "holding"),
        (1, None), (7, None), (None, None),
    ])
    def test_known_codes_map_and_unknown_ones_do_not(self, raw, expected):
        assert _entity(MSpaHeatingStateSensor, heat_state=raw).native_value == expected

    def test_every_state_is_a_declared_option(self):
        assert set(MSpaHeatingStateSensor._STATES.values()) == set(
            MSpaHeatingStateSensor._attr_options)


class TestPowerModel:
    def _power(self, options=None, **data):
        c = MagicMock()
        c._last_data = {"heater": "off", "filter": "off", "bubble": "off",
                        "jet": "off", "heat_state": 0, **data}
        return _calculate_total_power(c, _entry(options))

    def test_jets_add_the_jet_pump(self):
        assert self._power(jet="on") == DEFAULT_JET_POWER

    def test_jet_power_is_an_option(self):
        assert self._power({CONF_JET_POWER: 0}, jet="on") == 0

    @pytest.mark.parametrize("level, expected", [
        (1, DEFAULT_BUBBLE_POWER_L1),
        (2, DEFAULT_BUBBLE_POWER_L2),
        (3, DEFAULT_BUBBLE_POWER),
        (None, DEFAULT_BUBBLE_POWER),
        ("x", DEFAULT_BUBBLE_POWER),
    ])
    def test_bubbles_draw_what_their_level_draws(self, level, expected):
        assert self._power(bubble="on", bubble_level=level) == expected

    def test_a_lower_level_never_reads_above_full_speed(self):
        assert self._power({"bubble_power": 400}, bubble="on", bubble_level=2) == 400

    def test_level_power_is_an_option(self):
        assert self._power({CONF_BUBBLE_POWER_L1: 123},
                           bubble="on", bubble_level=1) == 123


class TestDiagnostics:
    def test_identifying_fields_are_redacted_at_any_depth(self):
        out = redact({"device_id": "abc", "nested": {"sn": "123", "list": [{"mac": "aa"}]},
                      "water_temperature": 38.5})
        assert out["device_id"] == REDACTED
        assert out["nested"]["sn"] == REDACTED
        assert out["nested"]["list"][0]["mac"] == REDACTED
        assert out["water_temperature"] == 38.5

    def test_empty_values_are_left_visibly_empty(self):
        assert redact({"sn": "", "mac": None}) == {"sn": "", "mac": None}

    def test_payload_carries_status_and_leaks_nothing(self):
        entry = MagicMock()
        entry.data = {"account_email": "owner@example.com", "password": "hash",
                      "region": "ROW", "device_id": "dev-1"}
        entry.options = {"pump_power": 60}
        coord = MagicMock()
        coord._last_data = {"water_temperature": 20.0, "safety_lock": 0}
        coord.device_detail = {"device_uuid": "uuid-1", "certificate": "PEM", "warning": "A0"}
        coord.fault_code = None
        coord.model = "F-AM063W"
        out = build_diagnostics(entry, coord)
        text = json.dumps(out, default=str)
        for secret in ("owner@example.com", "hash", "dev-1", "uuid-1", "PEM"):
            assert secret not in text
        assert out["status"]["water_temperature"] == 20.0

    def test_an_id_repeated_inside_another_field_is_scrubbed_too(self):
        """The cloud's storage keys are the device id with a prefix."""
        entry = MagicMock()
        entry.data = {"device_id": "b6580127abcdef"}
        entry.options = {}
        coord = MagicMock()
        coord._last_data = {}
        coord.device_id = "b6580127abcdef"
        coord.serial_number = None
        coord.mac_address = None
        coord.device_detail = {"mystery_key": "pd_6AI9NK_b6580127abcdef",
                               "activate_ip": "203.0.113.7", "country": "CZ"}
        out = build_diagnostics(entry, coord)
        text = json.dumps(out, default=str)
        assert "b6580127abcdef" not in text
        assert "203.0.113.7" not in text
        assert out["device_detail"]["mystery_key"] == f"pd_6AI9NK_{REDACTED}"
        assert out["device_detail"]["country"] == "CZ"
        assert out["entry"]["data"]["region"] == "ROW"
        assert out["entry"]["options"] == {"pump_power": 60}
        assert out["device_detail"]["warning"] == "A0"
        assert out["coordinator"]["model"] == "F-AM063W"


def _keys(node, prefix=""):
    if not isinstance(node, dict):
        return {prefix}
    out = set()
    for k, v in node.items():
        out |= _keys(v, f"{prefix}.{k}" if prefix else k)
    return out


class TestCzechTranslation:
    def test_it_translates_exactly_the_keys_that_exist(self):
        en = json.loads((_PKG / "strings.json").read_text(encoding="utf-8"))
        cs = json.loads((_PKG / "translations" / "cs.json").read_text(encoding="utf-8"))
        assert _keys(cs) == _keys(en)

    def test_placeholders_survive_translation(self):
        import re
        en = json.loads((_PKG / "strings.json").read_text(encoding="utf-8"))
        cs = json.loads((_PKG / "translations" / "cs.json").read_text(encoding="utf-8"))

        def walk(a, b, path=""):
            if isinstance(a, dict):
                for k in a:
                    walk(a[k], b[k], f"{path}.{k}")
            else:
                assert sorted(re.findall(r"\{\w+\}", a)) == sorted(
                    re.findall(r"\{\w+\}", b)), path
        walk(en, cs)


class TestClimateApiShapes:
    """The class declares the temperature API the running Home Assistant reads."""

    def _reload(self, native: bool):
        base = climate_mod.ClimateEntity
        had = hasattr(base, "native_current_temperature")
        if native and not had:
            base.native_current_temperature = None
        try:
            return importlib.reload(climate_mod).MSpaClimate
        finally:
            if native and not had:
                del base.native_current_temperature

    def _instance(self, cls):
        e = object.__new__(cls)
        e.coordinator = MagicMock()
        e.coordinator.last_data = {"water_temperature": 37.5, "target_temperature": 40.0}
        return e

    def test_legacy_home_assistant_gets_the_old_names(self):
        try:
            cls = self._reload(native=False)
            e = self._instance(cls)
            assert e.current_temperature == 37.5
            assert e.target_temperature == 40.0
            assert "_attr_temperature_unit" in vars(cls)
            assert "native_current_temperature" not in vars(cls)
        finally:
            importlib.reload(climate_mod)

    def test_current_home_assistant_gets_only_the_native_names(self):
        try:
            cls = self._reload(native=True)
            e = self._instance(cls)
            assert e.native_current_temperature == 37.5
            assert e.native_target_temperature == 40.0
            assert "_attr_native_temperature_unit" in vars(cls)
            # Providing the old names as well is what raises the deprecation warning.
            for old in ("current_temperature", "target_temperature", "_attr_temperature_unit"):
                assert old not in vars(cls)
        finally:
            importlib.reload(climate_mod)
