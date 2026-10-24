"""Unit tests for the Zinvolt driver (core zinvolt integration entities).

HA is faked with a minimal states/services stub, so no runtime is required.
Entity resolution is tested through the pure ``_match_entities`` helper.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.omnibattery.drivers import ZinvoltEntityDriver
from custom_components.omnibattery.drivers.zinvolt import _MIN_WRITE_INTERVAL_S


class _State:
    def __init__(self, state: str, attributes: dict | None = None):
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, table: dict[str, _State]):
        self._table = table

    def get(self, entity_id):
        return self._table.get(entity_id)


# Entity ids as the core integration creates them for a battery named "bat".
_ENTITIES = {
    "battery_soc": "sensor.bat_battery",
    "battery_power": "sensor.bat_power",
    "zinvolt_mode": "select.bat_mode",
    "set_charge_power": "number.bat_maximum_charge_power",
    "set_discharge_power": "number.bat_maximum_output",
}


def _driver(states: dict[str, str] | None = None, *, output_max: int = 2000) -> ZinvoltEntityDriver:
    table = {
        _ENTITIES["battery_soc"]: _State("57.2"),
        _ENTITIES["battery_power"]: _State("-305"),
        _ENTITIES["zinvolt_mode"]: _State("self_use"),
        _ENTITIES["set_charge_power"]: _State("2000", {"max": 2000}),
        _ENTITIES["set_discharge_power"]: _State("400", {"max": output_max}),
    }
    for key, value in (states or {}).items():
        table[_ENTITIES[key]] = _State(value, table[_ENTITIES[key]].attributes)
    hass = MagicMock()
    hass.states = _States(table)
    hass.services.async_call = AsyncMock()
    driver = ZinvoltEntityDriver(hass, "devid")
    driver._entities = dict(_ENTITIES)
    return driver


def _calls(driver) -> list[tuple]:
    return [
        (c.args[0], c.args[1], c.args[2]["entity_id"], c.args[2].get("value", c.args[2].get("option")))
        for c in driver.hass.services.async_call.await_args_list
    ]


def _age_last_write(driver, seconds: float = _MIN_WRITE_INTERVAL_S) -> None:
    driver._last_write_monotonic -= seconds


# --- entity resolution -------------------------------------------------------

def test_match_entities_by_unique_id_suffix():
    entries = [
        ("sensor", "ALG001.state_of_charge", "sensor.renamed_soc"),
        ("sensor", "ALG001.power", "sensor.bat_power"),
        ("select", "ALG001.mode", "select.bat_mode"),
        ("number", "ALG001.max_charge_power", "number.bat_maximum_charge_power"),
        ("number", "ALG001.max_output", "number.bat_maximum_output"),
        ("number", "ALG001.upper_threshold", "number.bat_maximum_charge_level"),
        ("binary_sensor", "ALG001.charge", "binary_sensor.bat_charge"),
    ]
    assert ZinvoltEntityDriver._match_entities(entries) == {
        "battery_soc": "sensor.renamed_soc",
        "battery_power": "sensor.bat_power",
        "zinvolt_mode": "select.bat_mode",
        "set_charge_power": "number.bat_maximum_charge_power",
        "set_discharge_power": "number.bat_maximum_output",
    }


def test_match_entities_ignores_battery_pack_units():
    # Pack sub-devices only carry problem binary sensors.
    entries = [("binary_sensor", "ALP2401.communication", "binary_sensor.battery_2_communication")]
    assert ZinvoltEntityDriver._match_entities(entries) == {}


# --- capabilities ------------------------------------------------------------

def test_capabilities_reflect_slow_cloud_actuator():
    caps = ZinvoltEntityDriver(MagicMock(), "dev", max_discharge_power_w=5000).capabilities
    assert caps.max_charge_power_w == 2000
    assert caps.max_discharge_power_w == 2000  # clamped to the hardware envelope
    assert caps.min_charge_power_w == 20
    assert caps.has_force_mode is False
    assert caps.hardware_soc_cutoff is False
    assert caps.has_energy_counters is False
    assert caps.has_nominal_capacity is False
    assert caps.setpoint_confirm_reliable is False
    assert caps.readback_latency_s > caps.actuator_latency_s >= 10
    assert caps.engage_grace_s >= 120


# --- telemetry ---------------------------------------------------------------

async def test_read_telemetry_decodes_states():
    driver = _driver({"zinvolt_mode": "fast_charge"})
    assert await driver.read_telemetry() == {
        "battery_soc": 57.2,
        "battery_power": -305,
        "zinvolt_mode": "fast_charge",
        "set_charge_power": 2000,
        "set_discharge_power": 400,
    }
    assert await driver.read_telemetry(["battery_soc"]) == {"battery_soc": 57.2}


async def test_read_telemetry_omits_unavailable_states():
    # A failed cloud poll marks the core entities unavailable.
    driver = _driver({"battery_soc": "unavailable", "battery_power": "unknown"})
    snapshot = await driver.read_telemetry(["battery_soc", "battery_power"])
    assert snapshot == {}


async def test_connect_requires_every_entity():
    driver = _driver()
    driver.resolve = MagicMock(return_value=(_ENTITIES, []))
    assert await driver.connect() is True

    driver.resolve = MagicMock(return_value=({}, ["set_charge_power"]))
    assert await driver.connect() is False


async def test_connect_fails_while_soc_unavailable():
    driver = _driver({"battery_soc": "unavailable"})
    driver.resolve = MagicMock(return_value=(_ENTITIES, []))
    assert await driver.connect() is False


# --- control -----------------------------------------------------------------

async def test_charge_writes_limit_then_fast_charge():
    driver = _driver()
    result = await driver.apply_setpoint(750)
    assert result.ok and result.net_power_w == 750 and not result.confirmed
    assert result.applied == {"zinvolt_mode": "fast_charge", "set_charge_power": 750}
    assert _calls(driver) == [
        ("number", "set_value", _ENTITIES["set_charge_power"], 750),
        ("select", "select_option", _ENTITIES["zinvolt_mode"], "fast_charge"),
    ]


async def test_discharge_writes_output_then_fast_discharge():
    driver = _driver()
    result = await driver.apply_setpoint(-300)
    assert result.net_power_w == -300
    assert result.applied["set_discharge_power"] == 300
    assert _calls(driver) == [
        ("number", "set_value", _ENTITIES["set_discharge_power"], 300),
        ("select", "select_option", _ENTITIES["zinvolt_mode"], "fast_discharge"),
    ]


async def test_idle_is_fast_charge_at_zero():
    driver = _driver()
    result = await driver.apply_setpoint(0)
    assert result.applied == {"zinvolt_mode": "fast_charge", "set_charge_power": 0}
    assert _calls(driver)[0] == ("number", "set_value", _ENTITIES["set_charge_power"], 0)


async def test_mode_already_active_is_not_rewritten():
    driver = _driver({"zinvolt_mode": "fast_charge"})
    await driver.apply_setpoint(500)
    assert _calls(driver) == [("number", "set_value", _ENTITIES["set_charge_power"], 500)]


async def test_command_is_clamped_to_entity_max_when_output_is_locked():
    driver = _driver(output_max=800)
    result = await driver.apply_setpoint(-1500)
    assert _calls(driver)[0] == ("number", "set_value", _ENTITIES["set_discharge_power"], 800)
    assert result.net_power_w == -1500  # within the envelope; the entity caps the wire value


async def test_failed_limit_write_skips_mode_and_reports_failure():
    driver = _driver()
    driver.hass.services.async_call.side_effect = RuntimeError("cloud down")
    result = await driver.apply_setpoint(-300)
    assert not result.ok and result.failure_reason == "service_call_failed"
    assert driver.hass.services.async_call.await_count == 1
    # Nothing was written, so the next request is not throttled.
    driver.hass.services.async_call.side_effect = None
    assert (await driver.apply_setpoint(-300)).ok
    assert driver.hass.services.async_call.await_count == 3


async def test_writes_are_throttled_and_report_the_held_command():
    driver = _driver()
    await driver.apply_setpoint(500)
    driver.hass.services.async_call.reset_mock()

    # Within the interval nothing is sent, even for a direction change.
    held = await driver.apply_setpoint(-800)
    assert driver.hass.services.async_call.await_count == 0
    assert held.ok and held.net_power_w == 500
    assert held.applied == {"zinvolt_mode": "fast_charge", "set_charge_power": 500}

    # After the interval a change inside the deadband is still skipped ...
    _age_last_write(driver)
    await driver.apply_setpoint(550)
    assert driver.hass.services.async_call.await_count == 0
    # ... but a larger change, or a direction change, goes out.
    assert (await driver.apply_setpoint(700)).net_power_w == 700
    _age_last_write(driver)
    assert (await driver.apply_setpoint(0)).net_power_w == 0


async def test_standby_writes_idle():
    driver = _driver()
    assert await driver.standby() is True
    assert _calls(driver)[0] == ("number", "set_value", _ENTITIES["set_charge_power"], 0)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"zinvolt_mode": "fast_charge", "set_charge_power": 600}, 600),
        ({"zinvolt_mode": "fast_charge", "set_charge_power": 0}, 0),
        ({"zinvolt_mode": "fast_discharge", "set_discharge_power": 300}, -300),
        ({"zinvolt_mode": "self_use", "set_charge_power": 600}, None),
        ({"zinvolt_mode": "fast_charge"}, None),
        ({}, None),
    ],
)
def test_net_power_from_data(data, expected):
    assert _driver().net_power_from_data(data) == expected


async def test_unsupported_controls_return_false():
    driver = _driver()
    assert await driver.write_control("anything", 1) is False
    assert await driver.set_charge_cutoff(90) is False
    assert await driver.set_rs485_control(True) is False
    assert await driver.apply_config(max_soc_pct=100, min_soc_pct=10,
                                     max_charge_power_w=2000, max_discharge_power_w=2000)
