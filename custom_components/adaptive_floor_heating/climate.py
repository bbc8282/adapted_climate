"""Expose standalone thermostat intent; related: runtime.py and controller.py."""

import json
from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.event import async_call_later

from .const import DIAGNOSTIC_WRITE_INTERVAL, DOMAIN, MAX_TARGET, MIN_TARGET, NAME
from .entity_naming import entity_id
from .curve_api import heating_curve_preview
from .diagnostics import learning_summary


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create one thermostat for the standalone runtime assigned to this entry."""
    async_add_entities([AdaptiveFloorHeatingClimate(entry, entry.runtime_data)])


class AdaptiveFloorHeatingClimate(ClimateEntity):
    """A non-polling thermostat driven by validated reports and monotonic timers."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None
    _attr_icon = "mdi:heating-coil"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_preset_modes = ["home", "away"]
    _attr_min_temp = MIN_TARGET
    _attr_max_temp = MAX_TARGET
    _attr_target_temperature_step = 0.1
    _attr_precision = 0.1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.PRESET_MODE
    )
    # Keep large, live-only diagnostics out of Recorder; the live state still carries them.
    # The panel reads current_temperature/temperature/heater_confirmed_on and the prediction
    # export reads off_prediction/last_off_prediction/last_peak_comparison/runtime_off_plan
    # from history, so those stay recorded. Related: frontend/panel.js loadHistory, compactPredictionHistory.
    _unrecorded_attributes = frozenset({
        "heating_curve_preview", "learning_summary", "curve_memory", "curve_learning_counts",
        "curve_rejected_counts", "runtime_learning_samples", "start_prediction",
        "last_cooling_prediction", "room_temperatures", "pending_rooms", "room_blend_until",
        "heater_entity_ids", "temperature_sensor_entity_id",
    })
    # Recorded sub-fields that change on every evaluation without changing the forecast itself.
    # They stay in the published attribute and are only left out of the write signature;
    # the deferred diagnostic write still publishes them within DIAGNOSTIC_WRITE_INTERVAL.
    _signature_volatile_fields: dict[str, frozenset[str]] = {
        "off_prediction": frozenset({"generated_at"}),
    }

    def __init__(self, entry, runtime) -> None:
        self._runtime = runtime
        # Write throttling state; related: _async_runtime_updated.
        self._last_signature: str | None = None
        self._last_write_at: float | None = None
        self._deferred_write_unsub = None
        self._prepared_attributes: dict[str, Any] | None = None
        self._write_stopped = False
        self._attr_unique_id = f"{entry.entry_id}_climate"
        self.entity_id = entity_id(runtime.hass, entry, "climate", "")
        # Register the software thermostat; related: const.py and config_flow.py.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or NAME,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._write_stopped = False
        self._last_signature = self._last_write_at = None
        self.async_on_remove(self._runtime.subscribe(self._async_runtime_updated))
        self.async_on_remove(self._cancel_deferred_write)
        await self._runtime.async_start()

    async def async_will_remove_from_hass(self) -> None:
        # Entity disable/removal also stops control, even without entry unloading.
        await self._runtime.async_stop()
        # Drop a pending diagnostic write after the final stop notification.
        self._write_stopped = True
        self._cancel_deferred_write()
        await super().async_will_remove_from_hass()

    # Single write path for runtime notifications: recorded changes are written at once,
    # unrecorded-only changes at most once per DIAGNOSTIC_WRITE_INTERVAL. The panel reads
    # hass.states (live, not Recorder), so diagnostics there lag by at most that interval.
    @callback
    def _async_runtime_updated(self) -> None:
        if self._write_stopped:
            return
        attributes = self._build_extra_state_attributes()
        signature = self._significant_signature(attributes)
        if signature != self._last_signature:
            self._write_state(signature, attributes)
            return
        if self._deferred_write_unsub is not None:
            return                                # the pending write publishes the latest values
        now = self.hass.loop.time()
        remaining = (0.0 if self._last_write_at is None
                     else self._last_write_at + DIAGNOSTIC_WRITE_INTERVAL - now)
        if remaining <= 0:
            self._write_state(signature, attributes)
            return
        self._deferred_write_unsub = async_call_later(self.hass, remaining, self._async_deferred_write)

    @callback
    def _async_deferred_write(self, _now=None) -> None:
        self._deferred_write_unsub = None
        if self._write_stopped:
            return
        attributes = self._build_extra_state_attributes()
        self._write_state(self._significant_signature(attributes), attributes)

    def _write_state(self, signature: str, attributes: dict[str, Any]) -> None:
        self._cancel_deferred_write()
        self._last_signature = signature
        self._last_write_at = self.hass.loop.time()
        # Reuse the attributes already built for the signature during this write.
        self._prepared_attributes = attributes
        try:
            self.async_write_ha_state()
        finally:
            self._prepared_attributes = None

    @callback
    def _cancel_deferred_write(self) -> None:
        if self._deferred_write_unsub is not None:
            self._deferred_write_unsub()
            self._deferred_write_unsub = None

    def _significant_signature(self, attributes: dict[str, Any]) -> str:
        """Serialize the state and every recorded attribute, minus volatile sub-fields."""
        if not self.available:
            return json.dumps(["unavailable"])
        unrecorded = self._unrecorded_attributes | getattr(
            self, "_entity_component_unrecorded_attributes", frozenset()
        )
        combined = {**(getattr(self, "state_attributes", None) or {}), **attributes}
        recorded = {}
        for key, value in combined.items():
            if key in unrecorded:
                continue
            volatile = self._signature_volatile_fields.get(key)
            if volatile and isinstance(value, dict):
                value = {field: item for field, item in value.items() if field not in volatile}
            recorded[key] = value
        return json.dumps([getattr(self, "state", None), recorded], sort_keys=True, default=str)

    @property
    def available(self) -> bool:
        model = self._runtime.controller
        return (self._runtime.started and not model.sensor_fault
                and model.temperature is not None
                and self._runtime.actuator.observed is not None)

    @property
    def current_temperature(self) -> float | None:
        return self._runtime.controller.temperature

    @property
    def target_temperature(self) -> float:
        return self._runtime.controller.target

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode(self._runtime.controller.mode)

    @property
    def hvac_action(self) -> HVACAction | None:
        observed = self._runtime.actuator.observed
        if not self.available or (self.hvac_mode == HVACMode.OFF and observed):
            return None
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        return HVACAction.HEATING if observed else HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        return self._runtime.preset

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._prepared_attributes is not None:
            return self._prepared_attributes
        return self._build_extra_state_attributes()

    def _build_extra_state_attributes(self) -> dict[str, Any]:
        runtime = self._runtime
        faults = sorted(runtime.controller.faults)
        if runtime.controller.sensor_fault:
            faults.append("sensor_fault")
        return {
            "control_state": runtime.decision.state,
            "faults": faults,
            "heater_entity_id": runtime.heater,
            "heater_entity_ids": list(runtime.heaters),
            "temperature_sensor_entity_id": runtime.sensor,
            "heater_confirmed_on": runtime.actuator.observed,
            "heater_command_pending": runtime.actuator.pending,
            "curve_fallback_reason": runtime.curve_fallback_reason,
            "hybrid_control_support": getattr(runtime, "hybrid_control_support", None),
            # Selected prediction and independent result comparison; related: runtime.py.
            "off_prediction": runtime.off_prediction,
            "start_prediction": runtime.start_prediction,
            "heating_prediction_info": runtime.heating_prediction_info,
            "prediction_on": runtime.prediction_mode != "eco",
            "sunrise_saving_enabled": runtime.sunrise_enabled,
            "sunrise_saving_active": runtime.sunrise_saving_active,
            # Daily target schedule and its AUTO pre-heat; related: runtime.py, schedule.py.
            **runtime.schedule_attributes(),
            # Measured rooms: per-room means, active set and blend end; related: rooms.py.
            **runtime.room_attributes(),
            "active_off_model": runtime.active_off_model,
            # Expose the fallback plan independently of the current model; related: runtime.py, frontend/panel.js.
            "runtime_off_plan": dict(runtime.runtime_off_plan) if runtime.runtime_off_plan else None,
            # Read-only learned reference at the current heating phase; related: curve_api.py, panel.js.
            "heating_curve_preview": heating_curve_preview(runtime),
            "last_off_prediction": runtime.last_off_prediction,
            "last_peak_comparison": runtime.last_peak_comparison,
            "last_cooling_prediction": runtime.last_cooling_prediction,
            # Read-only learning coverage and OFF model validation; related: diagnostics.py, panel.js.
            "learning_summary": learning_summary(runtime),
            "curve_memory": (
                {curve: memory.diagnostics() for curve, memory in runtime.curve_store.model.memory.items()}
                if runtime.curve_store is not None else {}
            ),
            "curve_learning_counts": (
                dict(runtime.curve_store.model.accepted) if runtime.curve_store is not None else {}
            ),
            "runtime_learning_samples": (
                len(runtime.curve_store.model.runtime_samples) if runtime.curve_store is not None else 0
            ),
            "curve_rejected_counts": (
                dict(runtime.curve_store.model.rejected) if runtime.curve_store is not None else {}
            ),
            "curve_last_quality_reason": (
                runtime.curve_store.model.last_reason if runtime.curve_store is not None else None
            ),
            "curve_phase": (
                "idle" if runtime.curve_tracker.cycle is None else
                "cooling" if runtime.curve_tracker.cycle.peak_at is not None else
                "post_heat_rise" if runtime.curve_tracker.cycle.off_at is not None else "heating"
            ),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        try:
            self._runtime.set_target(kwargs.get(ATTR_TEMPERATURE))
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        try:
            self._runtime.set_mode(hvac_mode)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        try:
            self._runtime.set_preset(preset_mode)
        except ValueError as err:
            raise ServiceValidationError(str(err)) from err

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
