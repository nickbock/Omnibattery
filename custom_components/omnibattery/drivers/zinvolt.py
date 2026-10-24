"""Home Assistant entity driver for Zinvolt batteries.

Zinvolt only offers a cloud API, which Home Assistant's core ``zinvolt``
integration already wraps (login, polling, token refresh). This driver reads
and writes that integration's entities instead of opening a second cloud
session, the same way the ESPHome driver rides on ESPHome's entities.

The battery has no signed power set-point; it has operating modes plus power
limits. The mapping, measured on a Zinvolt with 2 kW output:

  +W  Maximum charge power = W, mode Fast charge. The limit caps Fast charge.
  -W  Maximum output = W, mode Fast discharge. The limit caps Fast discharge.
   0  Maximum charge power = 0, mode Fast charge. The vendor app calls this
      state "Inactive"; its payload is identical.

The battery ramps at about 25 W/s after a dead time of 8-13 s, and the core
integration polls the cloud every 30 s. Both are slow next to the 2 s control
loop, so writes are throttled and the timing capabilities are conservative.
Requires Home Assistant 2026.11 or newer for the Maximum charge power entity.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .base import (
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)

# Hardware envelope reported by the cloud API (chargeMaxPower, and maxOutput
# once the output is unlocked). The live entity maxima clamp each write.
_MAX_CHARGE_POWER_W = 2000
_MAX_DISCHARGE_POWER_W = 2000
# chargeMinPower in the cloud API.
_MIN_CHARGE_POWER_W = 20

# The core integration's mode select options.
_MODE_CHARGE = "fast_charge"
_MODE_DISCHARGE = "fast_discharge"

# Logical key -> (HA domain, unique_id suffix). The core integration builds
# unique ids as "<serial>.<key>", which survives entity_id renames and does not
# depend on the user's language.
_ENTITY_MAP: dict[str, tuple[str, str]] = {
    "battery_soc": ("sensor", "state_of_charge"),
    "battery_power": ("sensor", "power"),
    "zinvolt_mode": ("select", "mode"),
    "set_charge_power": ("number", "max_charge_power"),
    "set_discharge_power": ("number", "max_output"),
}

# Every set-point is up to two cloud round trips and the battery needs most of
# a minute to reach a new target, so a 2 s PD tick must not become a 2 s write
# rate. Re-issue only on a meaningful change once the previous one had time to
# land; the interval matches the core integration's 30 s poll.
_WRITE_DEADBAND_W = 100
_MIN_WRITE_INTERVAL_S = 30.0

SENSOR_DEFINITIONS = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 1, "scan_interval": "medium", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": True},
]


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _echo(applied: int) -> dict:
    """The entity states a written set-point produces."""
    echo = {
        "zinvolt_mode": _MODE_DISCHARGE if applied < 0 else _MODE_CHARGE,
        "set_charge_power": max(applied, 0),
    }
    if applied < 0:
        echo["set_discharge_power"] = -applied
    return echo


class ZinvoltEntityDriver(BatteryDriver):
    """Drive one Zinvolt battery through the core zinvolt integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_id: str,
        *,
        max_charge_power_w: int = _MAX_CHARGE_POWER_W,
        max_discharge_power_w: int = _MAX_DISCHARGE_POWER_W,
    ) -> None:
        self.hass = hass
        self._device_id = device_id
        self._connected = False
        self._shutting_down = False
        self._entities: dict[str, str] = {}
        self._last_written_w: Optional[int] = None
        self._last_write_monotonic = 0.0
        self._capabilities = DriverCapabilities(
            # Upper/lower charge level numbers exist, but the control layer
            # owns the user's SOC window, as for the other entity drivers.
            hardware_soc_cutoff=False,
            has_force_mode=False,
            # read_telemetry returns HA's state cache, fed by the core poll.
            push_telemetry=True,
            max_charge_power_w=max(0, min(int(max_charge_power_w), _MAX_CHARGE_POWER_W)),
            max_discharge_power_w=max(0, min(int(max_discharge_power_w), _MAX_DISCHARGE_POWER_W)),
            # The core integration has no PV entity.
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=False,
            has_daily_energy_counters=False,
            has_nominal_capacity=False,
            setpoint_confirm_reliable=False,
            # Measured dead time 8-13 s before the battery starts moving.
            actuator_latency_s=15.0,
            # Dead time plus one 30 s core poll before the readback moves.
            readback_latency_s=45.0,
            # A full reversal from 2 kW charge keeps charging for about 80 s
            # while it ramps through zero, plus dead time and the poll.
            engage_grace_s=130.0,
            min_charge_power_w=_MIN_CHARGE_POWER_W,
        )
        self._read_groups = [
            ReadGroup("medium", tuple(_ENTITY_MAP)),
        ]

    # --- entity definitions ---------------------------------------------------

    @property
    def sensor_definitions(self) -> list[dict]: return SENSOR_DEFINITIONS
    @property
    def number_definitions(self) -> list[dict]: return []
    @property
    def select_definitions(self) -> list[dict]: return []
    @property
    def switch_definitions(self) -> list[dict]: return []
    @property
    def binary_sensor_definitions(self) -> list[dict]: return []
    @property
    def button_definitions(self) -> list[dict]: return []
    @property
    def all_definitions(self) -> list[dict]: return SENSOR_DEFINITIONS

    # --- identity -------------------------------------------------------------

    @property
    def capabilities(self) -> DriverCapabilities: return self._capabilities
    @property
    def model_label(self) -> Optional[str]: return "Zinvolt"

    # --- entity resolution ----------------------------------------------------

    @staticmethod
    def _match_entities(entries: list[tuple[str, str, str]]) -> dict[str, str]:
        """Map (domain, unique_id, entity_id) registry entries to logical keys."""
        by_suffix = {
            (domain, unique_id.rsplit(".", 1)[-1]): entity_id
            for domain, unique_id, entity_id in entries
        }
        return {
            key: by_suffix[target]
            for key, target in _ENTITY_MAP.items()
            if target in by_suffix
        }

    @classmethod
    def resolve(cls, hass: HomeAssistant, device_id: str) -> tuple[dict[str, str], list[str]]:
        """Return (logical key -> entity_id, missing keys) for a zinvolt device."""
        registry = er.async_get(hass)
        resolved = cls._match_entities([
            (entry.domain, entry.unique_id, entry.entity_id)
            for entry in er.async_entries_for_device(
                registry, device_id, include_disabled_entities=True
            )
            if entry.platform == "zinvolt"
        ])
        return resolved, sorted(_ENTITY_MAP.keys() - resolved.keys())

    # --- connection lifecycle ---------------------------------------------------

    @property
    def connected(self) -> bool: return self._connected

    async def connect(self) -> bool:
        resolved, missing = self.resolve(self.hass, self._device_id)
        if missing:
            if not self._shutting_down:
                _LOGGER.error(
                    "Zinvolt device %s is missing entities for %s (needs Home "
                    "Assistant 2026.11 or newer, with those entities enabled)",
                    self._device_id, missing,
                )
            self._connected = False
            return False
        self._entities = resolved
        soc = self.hass.states.get(resolved["battery_soc"])
        self._connected = soc is not None and soc.state not in ("unavailable", "unknown")
        if not self._connected and not self._shutting_down:
            _LOGGER.warning(
                "Zinvolt device %s found but %s is %s (battery or cloud offline?)",
                self._device_id, resolved["battery_soc"], soc.state if soc else "absent",
            )
        return self._connected

    async def close(self) -> None: self._connected = False
    def set_shutting_down(self, value: bool) -> None: self._shutting_down = value

    # --- telemetry (read) -------------------------------------------------------

    @property
    def read_groups(self) -> list[ReadGroup]: return self._read_groups

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        """Return the mapped entities' states; unavailable ones are omitted.

        The core integration marks its entities unavailable when a cloud poll
        fails, so a cloud outage empties the snapshot rather than replaying
        the last values.
        """
        snapshot: TelemetrySnapshot = {}
        for key in keys if keys is not None else list(self._entities):
            entity_id = self._entities.get(key)
            state = self.hass.states.get(entity_id) if entity_id else None
            if state is None or state.state in ("unavailable", "unknown"):
                continue
            if key == "zinvolt_mode":
                snapshot[key] = state.state
                continue
            try:
                value = float(state.state)
            except ValueError:
                continue
            snapshot[key] = int(value) if value.is_integer() else value
        return snapshot

    # --- control (write) --------------------------------------------------------

    def _should_write(self, applied: int) -> bool:
        """Whether this set-point is worth cloud writes and a new ramp."""
        if self._last_written_w is None:
            return True
        if asyncio.get_running_loop().time() - self._last_write_monotonic < _MIN_WRITE_INTERVAL_S:
            return False
        # Idle and direction changes skip the deadband, never the interval.
        if _sign(applied) != _sign(self._last_written_w):
            return True
        return abs(applied - self._last_written_w) >= _WRITE_DEADBAND_W

    async def _call(self, domain: str, service: str, entity_id: str, data: dict) -> bool:
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entity_id, **data}, blocking=True
            )
            return True
        except Exception as exc:
            if not self._shutting_down:
                _LOGGER.warning(
                    "Zinvolt driver: %s.%s on %s failed: %s", domain, service, entity_id, exc
                )
            return False

    async def _write(self, applied: int) -> bool:
        """Write the limit first, then the mode, so a failed mode write leaves
        the previous direction running at its old limit instead of a new
        direction at a stale one."""
        limit_key = "set_discharge_power" if applied < 0 else "set_charge_power"
        mode = _MODE_DISCHARGE if applied < 0 else _MODE_CHARGE
        limit_entity = self._entities.get(limit_key)
        mode_entity = self._entities.get("zinvolt_mode")
        if limit_entity is None or mode_entity is None:
            return False
        value = abs(applied)
        # The entity maximum follows the unit (800 W while output is locked).
        state = self.hass.states.get(limit_entity)
        entity_max = state.attributes.get("max") if state else None
        if entity_max is not None:
            value = min(value, int(entity_max))
        if not await self._call("number", "set_value", limit_entity, {"value": value}):
            return False
        mode_state = self.hass.states.get(mode_entity)
        if mode_state is not None and mode_state.state == mode:
            return True
        return await self._call("select", "select_option", mode_entity, {"option": mode})

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        applied = max(-self._capabilities.max_discharge_power_w,
                      min(self._capabilities.max_charge_power_w, int(net_power_w)))
        if not self._should_write(applied):
            # Nothing was sent, so report the command in force. Echoing the
            # request would make the older power look like non-delivery.
            held = self._last_written_w
            return SetpointResult(True, held, False, applied=_echo(held))
        if not await self._write(applied):
            return SetpointResult(False, applied, False, failure_reason="service_call_failed")
        self._last_written_w = applied
        self._last_write_monotonic = asyncio.get_running_loop().time()
        # The core integration refreshes after each write, but battery power
        # only moves after the dead time, so there is nothing to confirm yet.
        return SetpointResult(True, applied, False, applied=_echo(applied))

    async def write_control(self, key: str, value: int) -> bool: return False

    def net_power_from_data(self, data: dict) -> Optional[int]:
        mode = data.get("zinvolt_mode")
        if mode == _MODE_CHARGE and data.get("set_charge_power") is not None:
            return int(data["set_charge_power"])
        if mode == _MODE_DISCHARGE and data.get("set_discharge_power") is not None:
            return -int(data["set_discharge_power"])
        # Any other mode is the battery's own control, not a set-point of ours.
        return None

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({"zinvolt_mode", "set_charge_power", "set_discharge_power"})

    # --- concrete methods (not on BatteryDriver ABC) ----------------------------

    async def apply_config(self, **kwargs) -> bool: return True
    async def set_charge_cutoff(self, soc_pct: float) -> bool: return False
    async def set_rs485_control(self, enable: bool) -> bool: return False

    async def standby(self) -> bool:
        """Idle the battery (the app's "Inactive") before teardown."""
        return await self._write(0)
