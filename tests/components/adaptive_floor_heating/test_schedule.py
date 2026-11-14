"""Daily heating schedule tests with HA boundary doubles; related: schedule.py, runtime.py, switch.py, climate.py."""

import asyncio
from datetime import datetime, time as ClockTime, timedelta, timezone
import importlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.adaptive_floor_heating.controller import Settings
from custom_components.adaptive_floor_heating.curve_learning import CurveStandards
from custom_components.adaptive_floor_heating.runtime import HeatingRuntime
from custom_components.adaptive_floor_heating.schedule import (
    MAX_PREHEAT_LEAD_MINUTES, ScheduleSlot, active_slot, decode_slots, encode_slots,
    format_slots, next_slot, parse_slots, preheat_lead_minutes,
)
from .test_runtime import FakeBus, boundary_modules

SEOUL = timezone(timedelta(hours=9))


def at(day, hour, minute=0):
    """November 2026 in Seoul: the 14th is a Saturday, the 16th a Monday."""
    return datetime(2026, 11, day, hour, minute, tzinfo=SEOUL)


def learned_model(count=3):
    """Real runtime->Peak curve: 30/60/90 heater minutes lift 0.5/1.2/2.0 C after a 10 min lag."""
    model = CurveStandards()
    rows = [(30.0, 0.5, 10.0, time.time()), (60.0, 1.2, 10.0, time.time()), (90.0, 2.0, 10.0, time.time())]
    model.load_runtime_samples(rows[:count])
    return model


class SlotParsingTests(unittest.TestCase):
    def test_parses_lines_defaults_days_and_sorts(self):
        slots = parse_slots("21:00 25\n06:00, 20 weekday;\n 9:30 22.25 WEEKEND \n")
        self.assertEqual(slots, (
            ScheduleSlot(ClockTime(6), 20.0, "weekday"),
            ScheduleSlot(ClockTime(9, 30), 22.2, "weekend"),
            ScheduleSlot(ClockTime(21), 25.0, "all"),
        ))
        self.assertEqual(format_slots(slots), "06:00 20 weekday\n09:30 22.2 weekend\n21:00 25 all")
        self.assertEqual(decode_slots(encode_slots(slots)), slots)
        self.assertEqual(parse_slots(""), ())
        self.assertEqual(decode_slots(None), ())

    def test_rejects_invalid_slots_with_translation_keys(self):
        for text, error in (
            ("24:00 20", "schedule_invalid_time"), ("6-00 20", "schedule_invalid_time"),
            ("06:0 20", "schedule_invalid_time"), ("06:00 17.9", "schedule_invalid_temperature"),
            ("06:00 31", "schedule_invalid_temperature"), ("06:00 warm", "schedule_invalid_temperature"),
            ("06:00 20 monday", "schedule_invalid_days"), ("06:00", "schedule_invalid_format"),
            ("06:00 20 all\n06:00 21 weekday", "schedule_duplicate_time"),
            ("\n".join(f"{hour:02}:00 20" for hour in range(13)), "schedule_too_many"),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                parse_slots(text)
            self.assertEqual(str(caught.exception), error)
        # Weekday and weekend never overlap, so they may share a time.
        self.assertEqual(len(parse_slots("07:00 21 weekday\n07:00 22 weekend")), 2)
        with self.assertRaises(ValueError):
            decode_slots([{"time": "07:00", "temperature": True}])

    def test_active_and_next_wrap_midnight(self):
        slots = parse_slots("06:00 20\n21:00 25")
        active = active_slot(slots, at(17, 2))
        self.assertEqual((active.at, active.slot.temperature), (at(16, 21), 25.0))
        upcoming = next_slot(slots, at(17, 2))
        self.assertEqual((upcoming.at, upcoming.slot.temperature), (at(17, 6), 20.0))
        # The boundary itself is active, and the next one is strictly later.
        self.assertEqual(active_slot(slots, at(17, 6)).at, at(17, 6))
        self.assertEqual(next_slot(slots, at(17, 21)).at, at(18, 6))

    def test_weekday_and_weekend_day_types(self):
        slots = parse_slots("07:00 21 weekday\n09:00 22 weekend")
        self.assertEqual(active_slot(slots, at(16, 8)).at, at(16, 7))          # Monday
        self.assertEqual(active_slot(slots, at(14, 8)).at, at(13, 7))          # Saturday morning -> Friday
        self.assertEqual(next_slot(slots, at(14, 8)).at, at(14, 9))
        self.assertEqual(next_slot(slots, at(15, 23)).at, at(16, 7))           # Sunday night -> Monday
        self.assertIsNone(active_slot((), at(16, 8)))


class PreheatLeadTests(unittest.TestCase):
    def test_lead_uses_learned_runtime_curve_and_cap(self):
        lead = preheat_lead_minutes(learned_model(), 20.0, 21.0, None)
        self.assertAlmostEqual(lead, 30 + 0.5 * 30 / 0.7)
        self.assertIsNone(preheat_lead_minutes(learned_model(2), 20.0, 21.0, None))   # too few cycles
        self.assertIsNone(preheat_lead_minutes(learned_model(), 20.0, 22.5, None))    # beyond measured rise
        self.assertIsNone(preheat_lead_minutes(learned_model(), 21.0, 21.0, None))
        self.assertIsNone(preheat_lead_minutes(None, 20.0, 21.0, None))
        model = learned_model()
        model.load_runtime_samples([(100.0, 0.5, 10.0, None), (150.0, 1.2, 10.0, None), (200.0, 2.0, 10.0, None)])
        self.assertEqual(preheat_lead_minutes(model, 20.0, 21.0, None), MAX_PREHEAT_LEAD_MINUTES)


class FakeStore:
    def __init__(self, hass, version, key, **kwargs):
        self.hass, self.key = hass, key

    async def async_load(self):
        return self.hass.saved.get(self.key)

    def async_delay_save(self, producer, delay):
        self.hass.saved[self.key] = producer()

    async def async_save(self, data):
        self.hass.saved[self.key] = data

    async def async_remove(self):
        self.hass.saved.pop(self.key, None)


class ScheduleRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        modules = boundary_modules()
        modules["homeassistant.helpers.storage"].Store = FakeStore
        cls.module_patch = patch.dict(sys.modules, modules)
        cls.module_patch.start()
        cls.climate = importlib.import_module("custom_components.adaptive_floor_heating.climate")
        cls.switch = importlib.import_module("custom_components.adaptive_floor_heating.switch")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    async def asyncSetUp(self):
        self.states, self.calls = {}, []
        self.hass = SimpleNamespace(
            loop=asyncio.get_running_loop(), bus=FakeBus(), saved={}, data={},
            states=SimpleNamespace(get=self.states.get), config=SimpleNamespace(time_zone="Asia/Seoul"),
            services=SimpleNamespace(async_call=self.call),
        )
        self.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [self.entry])
        self.entry = SimpleNamespace(
            entry_id="one", title="Test thermostat",
            options={"minimum_on_time": 0, "minimum_off_time": 0,
                     "schedule_slots": encode_slots(parse_slots("06:00 20\n21:00 25"))},
            data={"mode": "standalone", "heater": "switch.heater", "temperature_sensor": "sensor.room"},
            runtime_data=None,
        )
        self.write("switch.heater", "off")
        self.write("sensor.room", "21", {"device_class": "temperature", "unit_of_measurement": "°C"})
        self.zone = patch("custom_components.adaptive_floor_heating.runtime.ZoneInfo", return_value=SEOUL)
        self.zone.start()
        self.addCleanup(self.zone.stop)

    def write(self, entity_id, value, attributes=None):
        previous = self.states.get(entity_id)
        attributes = attributes if attributes is not None else (previous.attributes if previous else {})
        now = datetime.now(SEOUL)
        state = SimpleNamespace(entity_id=entity_id, state=value, attributes=attributes,
                                last_reported=now, last_changed=now)
        self.states[entity_id] = state
        self.hass.bus.fire("state_changed", {"entity_id": entity_id, "new_state": state})

    async def call(self, domain, action, data, blocking):
        self.calls.append((domain, action, data["entity_id"]))
        self.write(data["entity_id"], "on" if action == "turn_on" else "off")

    async def start(self, wall, *, enabled=True, preset="home", mode="heat", target=22.0):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "target": target, "mode": mode, "faults": [], "preset": preset,
            "schedule_enabled": enabled,
        }
        runtime = HeatingRuntime(self.hass, self.entry, Settings(
            minimum_on_time=0, minimum_off_time=0, home_temperature=23.0, away_temperature=18.0))
        with patch("time.time", return_value=wall.timestamp()):
            await runtime.async_start()
        self.entry.runtime_data = runtime
        self.addAsyncCleanup(runtime._close)
        return runtime

    def tick(self, runtime, wall):
        with patch("time.time", return_value=wall.timestamp()):
            return runtime._schedule_tick(wall.timestamp())

    async def test_startup_applies_active_slot_only_when_enabled_and_home(self):
        runtime = await self.start(at(17, 2))
        self.assertEqual(runtime.controller.target, 25.0)                      # previous day's 21:00 slot
        self.assertEqual(self.hass.saved["adaptive_floor_heating.one.runtime"]["schedule_last_fire"],
                         at(16, 21).timestamp())
        await runtime._close()
        for enabled, preset in ((False, "home"), (True, "away")):
            runtime = await self.start(at(17, 2), enabled=enabled, preset=preset)
            self.assertEqual(runtime.controller.target, 22.0)
            await runtime._close()

    async def test_heat_applies_at_boundary_and_schedules_next_check(self):
        runtime = await self.start(at(17, 7))
        self.assertEqual(runtime.controller.target, 20.0)
        self.assertAlmostEqual(self.tick(runtime, at(17, 18)), 3600)          # 120 min pre-heat window start
        self.assertAlmostEqual(self.tick(runtime, at(17, 20)), 3600)          # boundary itself
        self.assertEqual(runtime.controller.target, 20.0)                      # HEAT never starts early
        self.tick(runtime, at(17, 21))
        self.assertEqual(runtime.controller.target, 25.0)
        with patch("time.time", return_value=at(17, 21, 5).timestamp()):
            attrs = self.climate.AdaptiveFloorHeatingClimate(self.entry, runtime).extra_state_attributes
        self.assertTrue(attrs["schedule_enabled"])
        self.assertEqual(attrs["schedule_next"], {"time": at(18, 6).isoformat(), "temperature": 20.0})
        self.assertEqual(attrs["schedule_active_slot"], {"time": "21:00", "temperature": 25.0, "days": "all"})
        self.assertIsNone(attrs["schedule_override_until"])

    async def test_away_skips_slots_and_home_restores_active_slot(self):
        runtime = await self.start(at(17, 7))
        with patch("time.time", return_value=at(17, 8).timestamp()):
            runtime.set_preset("away")
        self.assertEqual(runtime.controller.target, 18.0)
        self.tick(runtime, at(17, 21, 1))
        self.assertEqual(runtime.controller.target, 18.0)
        with patch("time.time", return_value=at(17, 22).timestamp()):
            runtime.set_preset("home")
        self.assertEqual(runtime.controller.target, 25.0)                      # not the 23 C HOME preset
        with patch("time.time", return_value=at(17, 22).timestamp()):
            runtime.set_schedule_enabled(False)
            runtime.set_preset("away")
            runtime.set_preset("home")
        self.assertEqual(runtime.controller.target, 23.0)

    async def test_manual_override_holds_until_next_boundary(self):
        runtime = await self.start(at(17, 7))
        with patch("time.time", return_value=at(17, 8).timestamp()):
            runtime.set_target(23.5)
        self.assertEqual(runtime.schedule_override_until, at(17, 21).timestamp())
        with patch("time.time", return_value=at(17, 8).timestamp()):
            self.assertEqual(runtime.schedule_attributes()["schedule_override_until"], at(17, 21).isoformat())
        self.tick(runtime, at(17, 20, 59))
        self.assertEqual(runtime.controller.target, 23.5)
        self.tick(runtime, at(17, 21))
        self.assertEqual(runtime.controller.target, 25.0)
        self.assertIsNone(runtime.schedule_override_until)
        # The override and boundary survive a restart without re-applying the slot.
        with patch("time.time", return_value=at(17, 22).timestamp()):
            runtime.set_target(24.0)
            runtime.evaluate()
        await runtime._close()
        restored = HeatingRuntime(self.hass, self.entry, runtime.controller.settings)
        with patch("time.time", return_value=at(17, 23).timestamp()):
            await restored.async_start()
        self.addAsyncCleanup(restored._close)
        self.assertEqual(restored.controller.target, 24.0)
        self.assertEqual(restored.schedule_override_until, at(18, 6).timestamp())

    async def test_switch_requires_slots_and_options_apply_live(self):
        runtime = await self.start(at(17, 7), enabled=False)
        entity = self.switch.ScheduleSwitch(self.entry, runtime)
        self.assertFalse(entity.is_on)
        with patch("time.time", return_value=at(17, 7).timestamp()):
            await entity.async_turn_on()
        self.assertTrue(entity.is_on)
        self.assertEqual(runtime.controller.target, 20.0)
        with patch("time.time", return_value=at(17, 7).timestamp()):
            runtime.set_schedule_slots(parse_slots("05:00 21.5 weekday"))
        self.assertEqual(runtime.controller.target, 21.5)
        runtime.set_schedule_slots(())
        self.assertFalse(entity.is_on)
        with self.assertRaises(sys.modules["homeassistant.exceptions"].ServiceValidationError):
            await entity.async_turn_on()

    async def test_auto_preheat_starts_by_learned_lead_only_with_evidence(self):
        lead = 30 + 0.5 * 30 / 0.7
        for samples, early in ((3, True), (2, False)):
            with self.subTest(samples=samples):
                runtime = await self.start(at(17, 7), mode="auto")
                runtime.curve_store = SimpleNamespace(model=learned_model(samples))
                runtime.controller.temperature = 24.0
                self.tick(runtime, at(17, 21) - timedelta(minutes=lead + 1))
                self.assertEqual(runtime.controller.target, 20.0)
                self.assertEqual(runtime.schedule_preheat["status"], "waiting" if early else "insufficient_evidence")
                self.tick(runtime, at(17, 21) - timedelta(minutes=lead - 1))
                self.assertEqual(runtime.controller.target, 25.0 if early else 20.0)
                self.tick(runtime, at(17, 21))
                self.assertEqual(runtime.controller.target, 25.0)
                await runtime._close()
        # Predictive ON off (eco) and HEAT both keep the slot time.
        for mode, prediction in (("auto", "eco"), ("heat", "comfort")):
            runtime = await self.start(at(17, 7), mode=mode)
            runtime.prediction_mode = prediction
            runtime.curve_store = SimpleNamespace(model=learned_model())
            runtime.controller.temperature = 24.0
            self.tick(runtime, at(17, 20, 59))
            self.assertEqual(runtime.controller.target, 20.0)
            await runtime._close()

    async def test_auto_early_stop_aims_at_lower_slot_inside_peak_delay(self):
        self.write("sensor.room", "26")
        runtime = await self.start(at(17, 22), mode="auto")
        self.assertEqual(runtime.controller.target, 25.0)
        model = learned_model()
        model.peak_delay["WARM_HEATING"] = (60.0, 5)
        runtime.curve_store = SimpleNamespace(model=model)
        heating = SimpleNamespace(curve_type="WARM_HEATING")
        with patch.object(runtime.curve_tracker, "cycle", heating):
            self.tick(runtime, at(18, 4, 30))
            self.assertEqual(runtime._predictive_off_target(), 25.5)           # 06:00 is beyond the delay
            self.tick(runtime, at(18, 5, 30))
            self.assertEqual(runtime._predictive_off_target(), 20.5)
            model.peak_delay["WARM_HEATING"] = (60.0, 2)                       # unconfident delay
            self.tick(runtime, at(18, 5, 30))
            self.assertEqual(runtime._predictive_off_target(), 25.5)
        runtime.curve_store = None


if __name__ == "__main__":
    unittest.main()
