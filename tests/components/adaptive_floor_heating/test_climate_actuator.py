"""Climate-entity heater support (boiler room controllers); related: runtime.py."""

import unittest
from types import SimpleNamespace

from custom_components.adaptive_floor_heating.const import (
    CLIMATE_OFF_TEMPERATURE,
    CLIMATE_ON_TEMPERATURE,
)
from custom_components.adaptive_floor_heating.runtime import (
    HeatingRuntime,
    climate_heater_state,
    climate_setpoint,
)


def climate(state="heat", temperature=None, **attrs):
    attributes = {"min_temp": 5, "max_temp": 40, **attrs}
    if temperature is not None:
        attributes["temperature"] = temperature
    return SimpleNamespace(entity_id="climate.boiler", state=state, attributes=attributes)


class ClimateSetpointTests(unittest.TestCase):
    def test_default_band_within_limits(self):
        self.assertEqual(climate_setpoint(climate(), True), CLIMATE_ON_TEMPERATURE)
        self.assertEqual(climate_setpoint(climate(), False), CLIMATE_OFF_TEMPERATURE)

    def test_band_is_clamped_to_entity_limits(self):
        narrow = climate(min_temp=15, max_temp=28)
        self.assertEqual(climate_setpoint(narrow, True), 28.0)
        self.assertEqual(climate_setpoint(narrow, False), 15.0)

    def test_missing_state_uses_defaults(self):
        self.assertEqual(climate_setpoint(None, True), CLIMATE_ON_TEMPERATURE)


class ClimateHeaterStateTests(unittest.TestCase):
    def test_on_and_off_follow_setpoint_band(self):
        self.assertIs(climate_heater_state(climate(temperature=30)), True)
        self.assertIs(climate_heater_state(climate(temperature=10)), False)

    def test_manual_setpoint_above_midpoint_reads_on(self):
        # A user raising the setpoint in the vendor app counts as heating demand.
        self.assertIs(climate_heater_state(climate(temperature=25)), True)

    def test_power_off_reads_off(self):
        self.assertIs(climate_heater_state(climate(state="off")), False)

    def test_unavailable_or_missing_setpoint_is_unknown(self):
        self.assertIsNone(climate_heater_state(climate(state="unavailable", temperature=30)))
        self.assertIsNone(climate_heater_state(climate(temperature=None)))

    def test_hot_water_band_reads_unknown(self):
        # BCM in away/hot-water mode reports a 30-60C hot-water setpoint.
        self.assertIsNone(climate_heater_state(climate(temperature=60, min_temp=30, max_temp=60)))

    def test_runtime_dispatches_by_entity_domain(self):
        self.assertIs(HeatingRuntime._switch_state(climate(temperature=30)), True)
        switch = SimpleNamespace(entity_id="switch.heater", state="on", attributes={})
        self.assertIs(HeatingRuntime._switch_state(switch), True)
        legacy = SimpleNamespace(state="off", attributes={})
        self.assertIs(HeatingRuntime._switch_state(legacy), False)


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, data))


class ClimateSendTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, heaters, states):
        runtime = HeatingRuntime.__new__(HeatingRuntime)
        runtime.heaters = heaters
        runtime.hass = SimpleNamespace(
            services=FakeServices(), states=SimpleNamespace(get=states.get)
        )
        return runtime

    async def test_on_raises_setpoint_and_powers_on_when_off(self):
        rt = self.runtime(("climate.boiler",), {"climate.boiler": climate(state="off", temperature=20)})
        await rt._send(True)
        self.assertEqual(rt.hass.services.calls, [
            ("climate", "set_hvac_mode", {"entity_id": "climate.boiler", "hvac_mode": "heat"}),
            ("climate", "set_temperature", {"entity_id": "climate.boiler", "temperature": 30.0}),
        ])

    async def test_off_lowers_setpoint_without_cutting_power(self):
        rt = self.runtime(("climate.boiler",), {"climate.boiler": climate(temperature=30)})
        await rt._send(False)
        self.assertEqual(rt.hass.services.calls, [
            ("climate", "set_temperature", {"entity_id": "climate.boiler", "temperature": 10.0}),
        ])

    async def test_hot_water_band_refuses_to_write(self):
        state = climate(temperature=60, min_temp=30, max_temp=60)
        rt = self.runtime(("climate.boiler",), {"climate.boiler": state})
        with self.assertRaises(RuntimeError):
            await rt._send(False)
        self.assertEqual(rt.hass.services.calls, [])

    async def test_switch_heaters_keep_original_service(self):
        rt = self.runtime(("switch.heater",), {})
        await rt._send(True)
        self.assertEqual(rt.hass.services.calls, [
            ("switch", "turn_on", {"entity_id": "switch.heater"}),
        ])


if __name__ == "__main__":
    unittest.main()
