"""Room aggregation, freshness, occupancy, blending and learning hygiene; related: rooms.py, runtime.py, config_flow.py."""

import asyncio
from datetime import UTC, datetime, timedelta
import importlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from custom_components.adaptive_floor_heating.const import ROOM_SET_CHANGED, ROOM_TRANSITION_SECONDS
from custom_components.adaptive_floor_heating.controller import Settings
from custom_components.adaptive_floor_heating.rooms import Room, RoomAggregator, decode_rooms
from custom_components.adaptive_floor_heating.runtime import HeatingRuntime, configured_rooms
from .test_config_flow import ha_boundaries
from .test_runtime import FakeBus, boundary_modules

TEMPERATURE = {"device_class": "temperature", "unit_of_measurement": "°C"}
LIVING = Room("living", ("sensor.l1", "sensor.l2"), "binary_sensor.living")
BED = Room("bed", ("sensor.b1",), "binary_sensor.bed")
STUDY = Room("study", ("sensor.s1",))


class AggregatorTests(unittest.TestCase):
    """Pure aggregate math on an injected monotonic clock."""

    def make(self, rooms=(LIVING, BED, STUDY), timeout=900):
        rooms_ = RoomAggregator(rooms, timeout)
        for entity, value in (("sensor.l1", 20.0), ("sensor.l2", 22.0), ("sensor.b1", 18.0), ("sensor.s1", 24.0)):
            rooms_.report_sensor(entity, value, 0.0)
        return rooms_

    def test_room_mean_and_all_rooms_without_occupancy(self):
        rooms = self.make()
        self.assertFalse(rooms.update(10))
        self.assertEqual(rooms.room_temperatures(10), {"living": 21.0, "bed": 18.0, "study": 24.0})
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))
        self.assertAlmostEqual(rooms.temperature, 21.0)

    def test_stale_and_invalid_sensors_are_excluded(self):
        rooms = self.make()
        rooms.report_sensor("sensor.l1", 21.0, 800)
        rooms.report_sensor("sensor.s1", None, 800)       # unavailable: stale at once
        rooms.update(950)                                   # l2 and b1 (reported at 0) expired
        self.assertEqual(rooms.room_temperatures(950), {"living": 21.0, "bed": None, "study": None})
        self.assertEqual(rooms.active_rooms, ("living",))
        rooms.update(1700)                                  # l1 expired too: nothing fresh
        self.assertIsNone(rooms.temperature)
        self.assertEqual(rooms.active_rooms, ())

    def test_occupied_rooms_take_precedence(self):
        rooms = self.make()
        rooms.report_occupancy("binary_sensor.bed", "on")
        rooms.report_occupancy("binary_sensor.living", "off")
        rooms.update(0)
        self.assertEqual(rooms.active_rooms, ("bed",))
        self.assertEqual(rooms.temperature, 18.0)
        rooms.report_occupancy("binary_sensor.living", "on")
        rooms.active_rooms = None                          # recompute without a blend
        rooms.update(0)
        self.assertEqual(rooms.active_rooms, ("living", "bed"))
        self.assertAlmostEqual(rooms.temperature, 19.5)
        # An occupied room without a fresh temperature does not count as occupied.
        rooms.report_sensor("sensor.b1", None, 0)
        rooms.report_sensor("sensor.l1", None, 0)
        rooms.report_sensor("sensor.l2", None, 0)
        rooms.active_rooms = None
        rooms.update(0)
        self.assertEqual(rooms.active_rooms, ("study",))
        rooms.report_occupancy("binary_sensor.bed", "unavailable")
        self.assertIsNone(rooms._occupied["binary_sensor.bed"])

    def test_fifteen_minute_linear_blend_and_mid_blend_restart(self):
        rooms = self.make()
        rooms.update(0)
        self.assertAlmostEqual(rooms.temperature, 21.0)
        rooms.report_occupancy("binary_sensor.bed", "on")
        self.assertTrue(rooms.update(100))                  # all (21.0) -> bed (18.0)
        self.assertAlmostEqual(rooms.temperature, 21.0)
        self.assertEqual(rooms.blend_until, 100 + ROOM_TRANSITION_SECONDS)
        rooms.report_sensor("sensor.b1", 18.0, 400)
        self.assertFalse(rooms.update(400))
        self.assertAlmostEqual(rooms.temperature, 21.0 - 3.0 * 300 / 900)   # 20.0
        self.assertTrue(rooms.blending(400))
        self.assertEqual(rooms.next_deadline(400), 460)     # blend tick before the 900 s expiry
        # Mid-blend change starts from the displayed 20.0 toward living (21.0).
        rooms.report_occupancy("binary_sensor.bed", "off")
        rooms.report_occupancy("binary_sensor.living", "on")
        rooms.report_sensor("sensor.l1", 20.0, 400)
        rooms.report_sensor("sensor.l2", 22.0, 400)
        self.assertTrue(rooms.update(400))
        self.assertAlmostEqual(rooms.temperature, 20.0)
        rooms.update(850)
        self.assertAlmostEqual(rooms.temperature, 20.5)
        rooms.report_sensor("sensor.l1", 20.0, 1200)
        rooms.report_sensor("sensor.l2", 22.0, 1200)
        rooms.update(1300)
        self.assertAlmostEqual(rooms.temperature, 21.0)
        self.assertFalse(rooms.blending(1300))

    def test_occupancy_dwell_ignores_flicker(self):
        rooms = self.make(timeout=100000)
        rooms.occupancy_dwell = 600
        rooms.report_occupancy("binary_sensor.bed", "off", 0)      # first report counts at once
        rooms.update(0)
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))
        rooms.report_occupancy("binary_sensor.bed", "on", 100)
        self.assertFalse(rooms.update(100))
        self.assertEqual(rooms.next_deadline(100), 700)          # dwell expiry wakes the runtime
        rooms.report_occupancy("binary_sensor.bed", "off", 400)    # flicker: the on-timer restarts
        rooms.update(800)
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))
        self.assertEqual(rooms.next_deadline(800), 100000)       # only sensor expiry, no dwell timer
        rooms.report_occupancy("binary_sensor.bed", "on", 1000)
        rooms.report_occupancy("binary_sensor.bed", "on", 1300)    # repeated on keeps the timer
        rooms.update(1599)
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))
        self.assertTrue(rooms.update(1600))
        self.assertEqual(rooms.active_rooms, ("bed",))
        rooms.report_occupancy("binary_sensor.bed", "unavailable", 2000)   # not on: leaves after the dwell
        rooms.update(2599)
        self.assertEqual(rooms.active_rooms, ("bed",))
        rooms.update(2600)
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))

    def test_hold_keeps_rooms_and_applies_pending_later(self):
        rooms = self.make()
        rooms.update(0)
        rooms.report_occupancy("binary_sensor.bed", "on")
        self.assertFalse(rooms.update(10, hold=True))
        self.assertEqual(rooms.active_rooms, ("living", "bed", "study"))
        self.assertEqual(rooms.pending_rooms, ("bed",))
        self.assertAlmostEqual(rooms.temperature, 21.0)
        self.assertFalse(rooms.blending(10))
        self.assertTrue(rooms.update(20))                          # released: 21.0 -> 18.0 blends
        self.assertEqual(rooms.active_rooms, ("bed",))
        self.assertIsNone(rooms.pending_rooms)
        self.assertTrue(rooms.blending(20))
        # A held room without a fresh temperature still drops out.
        rooms.report_occupancy("binary_sensor.bed", "off")
        rooms.active_rooms, rooms.temperature = ("living", "bed", "study"), 21.0
        rooms._blend_started = None
        rooms.report_sensor("sensor.b1", None, 30)
        rooms.update(30, hold=True)
        self.assertEqual(rooms.active_rooms, ("living", "study"))

    def test_small_switch_applies_at_once_without_learning_change(self):
        rooms = self.make(rooms=(LIVING, BED))
        for entity, value in (("sensor.l1", 20.0), ("sensor.l2", 20.4), ("sensor.b1", 20.0)):
            rooms.report_sensor(entity, value, 0.0)
        rooms.update(0)
        self.assertAlmostEqual(rooms.temperature, 20.1)
        rooms.report_occupancy("binary_sensor.living", "on")
        self.assertFalse(rooms.update(10))                         # 20.1 -> 20.2: within 0.4 °C
        self.assertEqual(rooms.active_rooms, ("living",))
        self.assertAlmostEqual(rooms.temperature, 20.2)
        self.assertFalse(rooms.blending(10))
        rooms.report_sensor("sensor.l2", 21.0, 15)                 # living 20.5
        rooms.update(15)
        rooms.report_occupancy("binary_sensor.living", "off")
        rooms.report_occupancy("binary_sensor.bed", "on")
        self.assertTrue(rooms.update(20))                          # 20.5 -> 20.0: blends
        self.assertTrue(rooms.blending(20))

    def test_no_blend_from_missing_temperature(self):
        rooms = self.make(rooms=(STUDY,))
        rooms.update(0)
        rooms.report_sensor("sensor.s1", None, 10)
        rooms.update(10)
        self.assertIsNone(rooms.temperature)
        rooms.report_sensor("sensor.s1", 23.0, 20)
        self.assertTrue(rooms.update(20))
        self.assertEqual(rooms.temperature, 23.0)
        self.assertFalse(rooms.blending(20))

    def test_decode_rejects_malformed_rooms(self):
        self.assertEqual(decode_rooms(None), ())
        self.assertEqual(decode_rooms([{"name": " A ", "sensors": ["sensor.a", "sensor.a"], "occupancy": None}]),
                         (Room("A", ("sensor.a",)),))
        for raw in ([{"name": "", "sensors": ["sensor.a"]}], [{"name": "A", "sensors": []}],
                    [{"name": "A", "sensors": ["switch.a"]}], [{"name": "A", "sensors": ["sensor.a"], "occupancy": "sensor.x"}],
                    [{"name": "A", "sensors": ["sensor.a"]}, {"name": "A", "sensors": ["sensor.b"]}],
                    "rooms", [{"name": "x", "sensors": ["sensor.a"]}] * 5):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_rooms(raw)

    def test_integrated_mode_ignores_rooms(self):
        rooms = [{"name": "A", "sensors": ["sensor.a"], "occupancy": None}]
        entry = SimpleNamespace(data={"mode": "multi_zone_integrated"}, options={"rooms": rooms})
        self.assertEqual(configured_rooms(entry), ())
        entry.data = {"mode": "standalone"}
        self.assertEqual(configured_rooms(entry), (Room("A", ("sensor.a",)),))
        entry.options = {"rooms": "broken"}
        with self.assertLogs(level="WARNING"):
            self.assertEqual(configured_rooms(entry), ())


class ShiftedLoop:
    """Expose the running loop with an adjustable monotonic offset."""

    def __init__(self, loop):
        self.loop, self.offset = loop, 0.0

    def time(self):
        return self.loop.time() + self.offset

    def call_later(self, delay, callback, *args):
        return self.loop.call_later(delay, callback, *args)

    def call_soon(self, callback, *args):
        return self.loop.call_soon(callback, *args)


class RuntimeRoomTests(unittest.IsolatedAsyncioTestCase):
    """Runtime integration with HA boundary doubles; related: test_runtime.py."""

    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, boundary_modules())
        cls.module_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    async def asyncSetUp(self):
        self.states, self.calls = {}, []
        self.loop = ShiftedLoop(asyncio.get_running_loop())
        self.hass = SimpleNamespace(
            loop=self.loop, bus=FakeBus(), saved={}, data={},
            states=SimpleNamespace(get=self.states.get), services=SimpleNamespace(async_call=self.call),
        )
        self.rooms = [
            {"name": "living", "sensors": ["sensor.l1", "sensor.l2"], "occupancy": "binary_sensor.living"},
            {"name": "bed", "sensors": ["sensor.b1"], "occupancy": None},
        ]
        self.entry = SimpleNamespace(
            entry_id="one", title="Test", options={"minimum_on_time": 0, "minimum_off_time": 0, "rooms": self.rooms},
            data={"mode": "standalone", "heater": "switch.heater", "temperature_sensor": "sensor.room"},
            runtime_data=None,
        )
        self.write("switch.heater", "off")
        for entity, value in (("sensor.room", "30"), ("sensor.l1", "20"), ("sensor.l2", "22"), ("sensor.b1", "18")):
            self.write(entity, value, TEMPERATURE)
        self.write("binary_sensor.living", "off", {})

    def no_dwell(self):
        self.entry.options = {**self.entry.options, "occupancy_dwell_minutes": 0}

    def write(self, entity_id, value, attributes=None):
        previous = self.states.get(entity_id)
        attributes = attributes if attributes is not None else (previous.attributes if previous else {})
        now = datetime.now(UTC)
        state = SimpleNamespace(entity_id=entity_id, state=value, attributes=attributes,
                                last_reported=now, last_changed=now)
        self.states[entity_id] = state
        kind = "state_reported" if previous is not None and previous.state == value else "state_changed"
        self.hass.bus.fire(kind, {"entity_id": entity_id, "new_state": state})

    async def call(self, domain, action, data, blocking):
        self.calls.append((domain, action, data["entity_id"]))
        self.write(data["entity_id"], "on" if action == "turn_on" else "off")

    async def start(self, settings=None, mode="off"):
        self.hass.saved["adaptive_floor_heating.one.runtime"] = {
            "schema_version": 1, "target": 21.0, "mode": mode, "faults": [], "preset": "home",
        }
        runtime = HeatingRuntime(self.hass, self.entry, settings or Settings(minimum_on_time=0, minimum_off_time=0))
        runtime.actuator._timeout = 0.01
        runtime.actuator._retry_delays = (0, 0)
        await runtime.async_start()
        self.addAsyncCleanup(runtime._close)
        return runtime

    async def settle(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def test_aggregate_replaces_single_sensor_and_exposes_attributes(self):
        runtime = await self.start()
        self.assertAlmostEqual(runtime.controller.temperature, 19.5)   # mean(living 21, bed 18), not sensor.room
        attrs = runtime.room_attributes()
        self.assertEqual(attrs["room_temperatures"], {"living": 21.0, "bed": 18.0})
        self.assertEqual(attrs["active_rooms"], ["living", "bed"])
        self.assertIsNone(attrs["room_blend_until"])
        listener = next(item for item in self.hass.bus.listeners if item[0] == "state_reported")
        for entity in ("sensor.l1", "sensor.b1", "binary_sensor.living", "switch.heater"):
            self.assertTrue(listener[2]({"entity_id": entity}), entity)
        self.assertFalse(listener[2]({"entity_id": "sensor.room"}))
        self.write("sensor.l1", "24")
        self.assertAlmostEqual(runtime.controller.temperature, (23 + 18) / 2)
        self.assertEqual(len(runtime.observation.history.samples), 1)

    async def test_one_reporting_sensor_keeps_aggregate_fresh(self):
        runtime = await self.start(Settings(minimum_on_time=0, minimum_off_time=0, sensor_timeout=900))
        for _ in range(4):                                  # 40 minutes, only sensor.b1 re-reports (unchanged)
            self.loop.offset += 600
            self.write("sensor.b1", "18")
            runtime.evaluate()
            self.assertFalse(runtime.controller.sensor_fault)
            self.assertIsNotNone(runtime.controller.temperature)
        self.assertEqual(runtime.room_attributes()["room_temperatures"], {"living": None, "bed": 18.0})
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["bed"])
        self.loop.offset += 901                             # now nothing is fresh
        runtime.evaluate()
        self.assertTrue(runtime.controller.sensor_fault)
        self.assertIsNone(runtime.controller.temperature)
        self.assertEqual(runtime.decision.state, "FAULT")
        self.write("sensor.l1", "20")                       # any report recovers the aggregate
        self.assertFalse(runtime.controller.sensor_fault)
        self.assertEqual(runtime.controller.temperature, 20.0)

    async def test_occupancy_blends_when_idle(self):
        self.no_dwell()
        runtime = await self.start()
        self.write("binary_sensor.living", "on")            # living only: 21.0
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["living"])
        self.assertAlmostEqual(runtime.controller.temperature, 19.5, places=3)   # blend starts at the shown value
        self.assertIsNotNone(runtime.room_attributes()["room_blend_until"])
        self.loop.offset += ROOM_TRANSITION_SECONDS / 3
        self.write("sensor.l1", "20")
        self.assertAlmostEqual(runtime.controller.temperature, 19.5 + 1.5 / 3, places=3)
        self.loop.offset += ROOM_TRANSITION_SECONDS
        self.write("sensor.l1", "20")
        self.write("sensor.l2", "22")
        self.assertAlmostEqual(runtime.controller.temperature, 21.0, places=3)
        self.assertIsNone(runtime.room_attributes()["room_blend_until"])

    async def test_default_dwell_ignores_brief_occupancy(self):
        runtime = await self.start()
        self.assertEqual(runtime.occupancy_dwell, 600)
        self.write("binary_sensor.living", "on")
        self.loop.offset += 300
        self.write("binary_sensor.living", "off")           # 5-minute flicker never counts
        self.loop.offset += 400
        runtime.evaluate()
        self.write("sensor.l1", "20")
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["living", "bed"])
        self.write("binary_sensor.living", "on")
        self.loop.offset += 601
        runtime.evaluate()
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["living"])
        runtime.set_occupancy_dwell(0)                       # live option edit
        self.assertEqual(runtime.room_aggregator.occupancy_dwell, 0)

    async def test_room_switch_waits_for_heating_cycle_and_peak(self):
        self.no_dwell()
        runtime = await self.start(Settings(minimum_on_time=0, minimum_off_time=0, sensor_timeout=10800,
                                            peak_observation_hours=1), mode="heat")
        await self.settle()
        self.assertTrue(runtime.actuator.observed)          # 19.5 < 21 - 0.5
        cycle = runtime.curve_tracker.cycle
        self.write("binary_sensor.living", "on")            # held while heating
        attrs = runtime.room_attributes()
        self.assertEqual(attrs["active_rooms"], ["living", "bed"])
        self.assertEqual(attrs["pending_rooms"], ["living"])
        self.assertIsNone(attrs["room_blend_until"])
        self.assertIsNone(cycle.invalid_reason)
        self.assertIsNotNone(runtime.observation.heating_started)
        self.loop.offset += 600
        for entity, value in (("sensor.l1", "24"), ("sensor.l2", "24"), ("sensor.b1", "20")):
            self.write(entity, value)
        await self.settle()
        self.assertFalse(runtime.actuator.observed)         # 22.0 reached the target
        self.assertIsNotNone(cycle.off_at)
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["living", "bed"])   # still held until the peak
        self.assertIsNone(cycle.invalid_reason)
        self.loop.offset += 3601                            # peak observation limit ends the hold
        wall = time.time
        with patch("time.time", lambda: wall() + self.loop.offset):    # curve clock uses wall time
            self.write("sensor.l1", "24")
        self.assertIsNone(runtime.curve_tracker.cycle)
        attrs = runtime.room_attributes()
        self.assertEqual(attrs["active_rooms"], ["living"])
        self.assertIsNone(attrs["pending_rooms"])
        self.assertIsNotNone(attrs["room_blend_until"])     # 22.0 -> 24.0 blends

    async def test_small_reference_switch_does_not_exclude_learning(self):
        self.no_dwell()
        for entity, value in (("sensor.l1", "19.4"), ("sensor.l2", "19.6"), ("sensor.b1", "19.3")):
            self.write(entity, value)
        runtime = await self.start()
        with patch.object(runtime, "_exclude_room_transition") as exclude:
            self.write("binary_sensor.living", "on")
            self.assertEqual(runtime.room_attributes()["active_rooms"], ["living"])
            self.assertIsNone(runtime.room_attributes()["room_blend_until"])
            self.assertAlmostEqual(runtime.controller.temperature, 19.5, places=3)
            exclude.assert_not_called()

    async def test_cycle_started_during_blend_is_excluded(self):
        self.no_dwell()
        runtime = await self.start()
        self.write("binary_sensor.living", "on")
        runtime.set_mode("heat")                            # starts heating mid-blend
        await self.settle()
        self.assertTrue(runtime.actuator.observed)
        self.assertEqual(runtime.curve_tracker.cycle.invalid_reason, ROOM_SET_CHANGED)
        self.assertIsNone(runtime.observation.heating_started)

    async def test_live_room_edit_blends_from_previous_value(self):
        runtime = await self.start()
        runtime.set_rooms(decode_rooms([{"name": "bed", "sensors": ["sensor.b1"], "occupancy": None}]))
        self.assertAlmostEqual(runtime.controller.temperature, 19.5, places=3)
        self.assertEqual(runtime.room_attributes()["active_rooms"], ["bed"])
        self.loop.offset += ROOM_TRANSITION_SECONDS
        self.write("sensor.b1", "18")
        self.assertAlmostEqual(runtime.controller.temperature, 18.0, places=3)
        runtime.set_rooms(())                               # back to the single data sensor
        self.assertIsNone(runtime.room_aggregator)
        self.assertEqual(runtime.controller.temperature, 30.0)

    async def test_single_sensor_entries_are_unchanged(self):
        self.entry.options = {"minimum_on_time": 0, "minimum_off_time": 0}
        runtime = await self.start(Settings(minimum_on_time=0, minimum_off_time=0, sensor_timeout=900))
        self.assertIsNone(runtime.room_aggregator)
        self.assertEqual(runtime.controller.temperature, 30.0)
        self.assertEqual(runtime.room_attributes(), {
            "room_temperatures": {"sensor.room": 30.0}, "active_rooms": ["sensor.room"],
            "pending_rooms": None, "room_blend_until": None,
        })
        listener = next(item for item in self.hass.bus.listeners if item[0] == "state_reported")
        self.assertTrue(listener[2]({"entity_id": "sensor.room"}))
        self.assertFalse(listener[2]({"entity_id": "sensor.l1"}))
        self.write("sensor.l1", "10")
        self.assertEqual(runtime.controller.temperature, 30.0)
        # Held valid state stays usable without reports, as before rooms existed.
        self.loop.offset += 2000
        runtime.evaluate()
        self.assertFalse(runtime.controller.sensor_fault)
        self.assertEqual(runtime.controller.temperature, 30.0)


class RoomsOptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    """Options-flow rooms step validation with HA boundary doubles; related: test_config_flow.py."""

    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, ha_boundaries())
        cls.module_patch.start()
        sys.modules.pop("custom_components.adaptive_floor_heating.config_flow", None)
        cls.flow_module = importlib.import_module("custom_components.adaptive_floor_heating.config_flow")

    @classmethod
    def tearDownClass(cls):
        cls.module_patch.stop()

    def setUp(self):
        self.states = {
            "sensor.a": SimpleNamespace(state="21.5", attributes=dict(TEMPERATURE)),
            "sensor.b": SimpleNamespace(state="70", attributes={"device_class": "temperature", "unit_of_measurement": "°F"}),
            "sensor.humidity": SimpleNamespace(state="40", attributes={"device_class": "humidity", "unit_of_measurement": "%"}),
            "sensor.kelvin": SimpleNamespace(state="290", attributes={"device_class": "temperature", "unit_of_measurement": "K"}),
            "binary_sensor.occ": SimpleNamespace(state="off", attributes={}),
        }
        self.hass = SimpleNamespace(states=SimpleNamespace(get=self.states.get), entries=[], progress=[],
                                    services=SimpleNamespace(async_call=AsyncMock()))

    async def flow_at_rooms(self, options=None, data=None):
        flow = self.flow_module.ConfigFlow.async_get_options_flow(None)
        flow.config_entry = SimpleNamespace(options=options or {}, data=data or {"mode": "standalone"})
        flow.hass = self.hass
        await flow.async_step_init({})
        await flow.async_step_preset_temperatures({})
        return flow, await flow.async_step_optional_sensors({})

    async def test_step_order_schema_and_saved_rooms(self):
        flow, form = await self.flow_at_rooms()
        self.assertEqual(form["step_id"], "rooms")
        keys = [str(key) for key in form["data_schema"].schema]
        self.assertEqual(keys, [f"room_{i}_{field}" for i in range(1, 5) for field in ("name", "sensors", "occupancy")]
                         + ["occupancy_dwell_minutes"])
        sensors = form["data_schema"].schema
        field = next(value for key, value in sensors.items() if str(key) == "room_1_sensors")
        self.assertEqual(field.config, {"domain": "sensor", "device_class": "temperature", "multiple": True})
        result = await flow.async_step_rooms({
            "room_1_name": " Living ", "room_1_sensors": ["sensor.a", "sensor.b"], "room_1_occupancy": "binary_sensor.occ",
            "room_3_name": "Bed", "room_3_sensors": ["sensor.a"],
        })
        self.assertEqual(result["step_id"], "schedule")
        result = await flow.async_step_schedule({"schedule_text": ""})
        self.assertIn("Living: sensor.a, sensor.b (binary_sensor.occ)", result["description_placeholders"]["rooms"])
        result = await flow.async_step_confirm({})
        self.assertEqual(result["data"]["rooms"], [
            {"name": "Living", "sensors": ["sensor.a", "sensor.b"], "occupancy": "binary_sensor.occ"},
            {"name": "Bed", "sensors": ["sensor.a"], "occupancy": None},
        ])
        self.assertEqual(len(decode_rooms(result["data"]["rooms"])), 2)
        self.assertEqual(result["data"]["occupancy_dwell_minutes"], 10)   # default for older entries

    async def test_all_empty_falls_back_and_existing_rooms_are_suggested(self):
        stored = [{"name": "Living", "sensors": ["sensor.a"], "occupancy": None}]
        flow, form = await self.flow_at_rooms({"rooms": stored})
        suggested = {str(key): key.description for key in form["data_schema"].schema}
        self.assertEqual(suggested["room_1_name"], {"suggested_value": "Living"})
        self.assertEqual(suggested["room_1_sensors"], {"suggested_value": ["sensor.a"]})
        self.assertIsNone(suggested["room_2_name"])
        result = await flow.async_step_rooms({"room_2_name": "", "room_2_sensors": []})
        self.assertEqual(result["step_id"], "schedule")
        self.assertEqual(flow._options_draft["rooms"], [])

    async def test_validation_errors_are_reported_per_field(self):
        flow, _ = await self.flow_at_rooms()
        cases = (
            ({"room_1_sensors": ["sensor.a"]}, {"room_1_name": "room_name_required"}),
            ({"room_1_name": "A"}, {"room_1_sensors": "room_sensors_required"}),
            ({"room_2_name": "A", "room_2_occupancy": "binary_sensor.occ"}, {"room_2_sensors": "room_sensors_required"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.a", "sensor.humidity"]}, {"room_1_sensors": "invalid_temperature"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.kelvin"]}, {"room_1_sensors": "invalid_unit"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.missing"]}, {"room_1_sensors": "invalid_entity"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.a"], "room_1_occupancy": "sensor.a"},
             {"room_1_occupancy": "invalid_occupancy"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.a"], "room_1_occupancy": "binary_sensor.missing"},
             {"room_1_occupancy": "invalid_entity"}),
            ({"room_1_name": "A", "room_1_sensors": ["sensor.a"], "room_4_name": "A", "room_4_sensors": ["sensor.b"]},
             {"room_4_name": "duplicate_room_name"}),
        )
        for user_input, errors in cases:
            with self.subTest(user_input=user_input):
                result = await flow.async_step_rooms(user_input)
                self.assertEqual(result["step_id"], "rooms")
                self.assertEqual(result["errors"], errors)
        result = await flow.async_step_rooms({"room_1_name": "A", "room_1_sensors": ["sensor.a"],
                                              "occupancy_dwell_minutes": 121})
        self.assertEqual(result["errors"], {"occupancy_dwell_minutes": "invalid_setting"})
        result = await flow.async_step_rooms({"room_1_name": "A", "room_1_sensors": ["sensor.a"],
                                              "occupancy_dwell_minutes": 3.0})
        self.assertEqual(result["step_id"], "schedule")
        self.assertEqual(flow._options_draft["occupancy_dwell_minutes"], 3)
        self.states["sensor.a"].state = "unavailable"
        result = await flow.async_step_rooms({"room_1_name": "A", "room_1_sensors": ["sensor.a"]})
        self.assertEqual(result["errors"], {"room_1_sensors": "entity_unavailable"})

    async def test_integrated_entries_skip_rooms(self):
        _, result = await self.flow_at_rooms(data={"mode": "multi_zone_integrated"})
        self.assertEqual(result["step_id"], "schedule")


if __name__ == "__main__":
    unittest.main()
