"""Climate state write throttling for unrecorded diagnostics; related: climate.py, const.py."""

import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .test_runtime import boundary_modules


class FakeHandle:
    def __init__(self, loop, when, action, args):
        self.loop, self.when, self.action, self.args = loop, when, action, args
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeLoop:
    """Manual clock: advance() runs due call_later callbacks in order."""

    def __init__(self):
        self.now = 1000.0
        self.handles = []

    def time(self):
        return self.now

    def call_later(self, delay, action, *args):
        handle = FakeHandle(self, self.now + delay, action, args)
        self.handles.append(handle)
        return handle

    def pending(self):
        return [handle for handle in self.handles if not handle.cancelled]

    def advance(self, seconds):
        self.now += seconds
        for handle in sorted(self.pending(), key=lambda item: item.when):
            if handle.when <= self.now and not handle.cancelled:
                handle.cancelled = True
                handle.action(*handle.args)


class FakeRuntime:
    def __init__(self, hass):
        self.hass = hass
        self.listeners = set()
        self.stop_notifies = False

    def subscribe(self, listener):
        self.listeners.add(listener)
        return lambda: self.listeners.discard(listener)

    def notify(self):
        for listener in tuple(self.listeners):
            listener()

    async def async_start(self):
        return None

    async def async_stop(self):
        if self.stop_notifies:
            self.notify()
        return True


class ClimateWriteThrottleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module_patch = patch.dict(sys.modules, boundary_modules())
        cls.module_patch.start()
        sys.modules.pop("custom_components.adaptive_floor_heating.climate", None)
        cls.climate = importlib.import_module("custom_components.adaptive_floor_heating.climate")
        cls.const = importlib.import_module("custom_components.adaptive_floor_heating.const")

        class Entity(cls.climate.AdaptiveFloorHeatingClimate):
            """Stand in for HA's state/state_attributes and the runtime-derived attributes."""
            attrs: dict = {}
            hvac = "heat"
            current = 20.0

            @property
            def available(self):
                return True

            @property
            def state(self):
                return self.hvac

            @property
            def state_attributes(self):
                return {"current_temperature": self.current, "hvac_modes": ["off", "heat", "auto"]}

            def _build_extra_state_attributes(self):
                return dict(self.attrs)

            def async_write_ha_state(self):
                self.written.append(dict(self.extra_state_attributes))

        cls.Entity = Entity

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("custom_components.adaptive_floor_heating.climate", None)
        cls.module_patch.stop()

    async def asyncSetUp(self):
        self.loop = FakeLoop()
        self.hass = SimpleNamespace(
            loop=self.loop,
            config_entries=SimpleNamespace(async_entries=lambda domain: []),
        )
        self.runtime = FakeRuntime(self.hass)
        entry = SimpleNamespace(entry_id="one", title="Test", data={"mode": "standalone"})
        self.entity = self.Entity(entry, self.runtime)
        self.entity.hass = self.hass
        self.entity.written = []
        self.entity.attrs = {"control_state": "IDLE", "room_temperatures": {"a": 20.01},
                             "off_prediction": None}
        await self.entity.async_added_to_hass()
        self.runtime.notify()                    # first notification always writes
        self.assertEqual(len(self.entity.written), 1)

    def set_attr(self, **values):
        self.entity.attrs = {**self.entity.attrs, **values}

    async def test_recorded_change_writes_immediately(self):
        self.loop.advance(1)
        self.set_attr(control_state="HEATING")
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 2)
        self.loop.advance(1)
        self.entity.current = 20.1               # HA base attribute (current_temperature)
        self.runtime.notify()
        self.loop.advance(1)
        self.entity.hvac = "auto"                # state itself
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 4)
        self.assertEqual(self.loop.pending(), [])

    async def test_diagnostic_only_changes_coalesce_into_one_deferred_write(self):
        interval = self.const.DIAGNOSTIC_WRITE_INTERVAL
        self.assertEqual(interval, 60)
        for second in range(1, 4):
            self.loop.advance(15)
            self.set_attr(room_temperatures={"a": 20 + second / 100})
            self.runtime.notify()
        self.assertEqual(len(self.entity.written), 1)
        self.assertEqual(len(self.loop.pending()), 1)   # one timer, not replaced per tick
        self.loop.advance(14)                           # t = 59 s since the last write
        self.assertEqual(len(self.entity.written), 1)
        self.loop.advance(1)                            # t = 60 s
        self.assertEqual(len(self.entity.written), 2)
        self.assertEqual(self.entity.written[-1]["room_temperatures"], {"a": 20.03})
        self.assertEqual(self.loop.pending(), [])
        # After a quiet interval a diagnostic change is written at once.
        self.loop.advance(interval)
        self.set_attr(room_temperatures={"a": 21.0})
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 3)

    async def test_significant_write_cancels_pending_diagnostic_write(self):
        self.loop.advance(10)
        self.set_attr(room_temperatures={"a": 20.5})
        self.runtime.notify()
        self.assertEqual(len(self.loop.pending()), 1)
        self.loop.advance(10)
        self.set_attr(control_state="HEATING")
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 2)
        self.assertEqual(self.entity.written[-1]["room_temperatures"], {"a": 20.5})
        self.assertEqual(self.loop.pending(), [])

    async def test_volatile_forecast_timestamp_does_not_force_a_write(self):
        forecast = {"model": "curve", "predicted_peak": 21.0, "generated_at": 1.0}
        self.loop.advance(1)
        self.set_attr(off_prediction=forecast)
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 2)
        self.loop.advance(15)
        self.set_attr(off_prediction={**forecast, "generated_at": 16.0})
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 2)           # deferred, not immediate
        self.loop.advance(15)
        self.set_attr(off_prediction={**forecast, "generated_at": 31.0, "predicted_peak": 21.2})
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), 3)           # forecast content changed
        self.assertEqual(self.entity.written[-1]["off_prediction"]["generated_at"], 31.0)

    async def test_removal_cancels_timer_and_ignores_late_notifications(self):
        self.loop.advance(5)
        self.set_attr(room_temperatures={"a": 22.0})
        self.runtime.notify()
        handle, = self.loop.pending()
        self.runtime.stop_notifies = True
        await self.entity.async_will_remove_from_hass()
        self.assertTrue(handle.cancelled)
        self.assertEqual(self.loop.pending(), [])
        self.assertIsNone(self.entity._deferred_write_unsub)
        written = len(self.entity.written)
        self.loop.advance(120)
        self.runtime.notify()
        self.assertEqual(len(self.entity.written), written)
