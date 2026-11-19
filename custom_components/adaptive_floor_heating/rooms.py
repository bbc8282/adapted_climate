"""Aggregate room sensors into one measured temperature; related: runtime.py, config_flow.py, const.py.

Pure logic on an injected monotonic clock. A room's temperature is the mean of
its fresh sensors; occupied rooms take precedence over all rooms. A room counts
as occupied only after its occupancy sensor stayed ``on`` for the dwell time and
stops counting only after it stayed off for the dwell time. A change of the
active-room set blends linearly from the last displayed value to the new
aggregate over ``ROOM_TRANSITION_SECONDS``, unless it moves the reference by
less than ``ROOM_SWITCH_MIN_DELTA`` (then it applies at once and is not reported
as a learning-relevant change). ``update(hold=True)`` keeps the current set
while the heater runs; the selected set is kept as ``pending_rooms``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .const import (
    CONF_ROOM_NAME, CONF_ROOM_OCCUPANCY, CONF_ROOM_SENSORS, MAX_ROOM_SLOTS,
    ROOM_SWITCH_MIN_DELTA, ROOM_TRANSITION_SECONDS,
)

# Re-evaluate a running blend at least this often so the display keeps moving.
BLEND_TICK_SECONDS = 60.0


@dataclass(frozen=True)
class Room:
    """One named room: averaged temperature sensors and an optional occupancy sensor."""

    name: str
    sensors: tuple[str, ...]
    occupancy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {CONF_ROOM_NAME: self.name, CONF_ROOM_SENSORS: list(self.sensors),
                CONF_ROOM_OCCUPANCY: self.occupancy}


def decode_rooms(raw: Any) -> tuple[Room, ...]:
    """Decode the stored option list; raise ValueError on any malformed room."""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_ROOM_SLOTS:
        raise ValueError("rooms")
    rooms, names = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("rooms")
        name = item.get(CONF_ROOM_NAME)
        sensors = item.get(CONF_ROOM_SENSORS)
        occupancy = item.get(CONF_ROOM_OCCUPANCY) or None
        if (not isinstance(name, str) or not name.strip() or name.strip() in names
                or not isinstance(sensors, (list, tuple)) or not sensors
                or any(not isinstance(s, str) or not s.startswith("sensor.") for s in sensors)
                or (occupancy is not None and (not isinstance(occupancy, str)
                                               or not occupancy.startswith("binary_sensor.")))):
            raise ValueError("rooms")
        names.add(name.strip())
        rooms.append(Room(name.strip(), tuple(dict.fromkeys(sensors)), occupancy))
    return tuple(rooms)


def encode_rooms(rooms) -> list[dict[str, Any]]:
    return [room.as_dict() for room in rooms]


def _mean(values) -> float | None:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


class RoomAggregator:
    """Track per-sensor reports and occupancy; produce the blended aggregate."""

    def __init__(self, rooms, timeout: float,
                 transition: float = ROOM_TRANSITION_SECONDS, *,
                 occupancy_dwell: float = 0.0, min_delta: float = ROOM_SWITCH_MIN_DELTA) -> None:
        self.rooms: tuple[Room, ...] = tuple(rooms)
        self.timeout = timeout
        self.transition = transition
        self.occupancy_dwell = max(0.0, float(occupancy_dwell))
        self.min_delta = min_delta
        self.sensors = tuple(dict.fromkeys(s for room in self.rooms for s in room.sensors))
        self.occupancy_sensors = tuple(dict.fromkeys(
            room.occupancy for room in self.rooms if room.occupancy))
        self.entities = frozenset((*self.sensors, *self.occupancy_sensors))
        self._values: dict[str, float | None] = {entity: None for entity in self.sensors}
        self._reported_at: dict[str, float | None] = {entity: None for entity in self.sensors}
        # Raw sensor state, when its on/not-on value last flipped, and the dwell-filtered value.
        self._occupied: dict[str, bool | None] = {entity: None for entity in self.occupancy_sensors}
        self._occupied_since: dict[str, float | None] = {entity: None for entity in self.occupancy_sensors}
        self._counted: dict[str, bool] = {entity: False for entity in self.occupancy_sensors}
        self.active_rooms: tuple[str, ...] | None = None
        # Selected set waiting while ``update(hold=True)`` keeps the current one.
        self.pending_rooms: tuple[str, ...] | None = None
        self.temperature: float | None = None
        self._blend_from: float | None = None
        self._blend_started: float | None = None

    # Inputs -----------------------------------------------------------------
    def report_sensor(self, entity_id: str, temperature: float | None, reported_at: float | None) -> None:
        """Record one HA report; an invalid value makes this sensor stale immediately."""
        if entity_id not in self._values:
            return
        valid = temperature is not None and isfinite(temperature)
        self._values[entity_id] = temperature if valid else None
        self._reported_at[entity_id] = reported_at if valid else None

    def report_occupancy(self, entity_id: str, state: str | None, now: float = 0.0) -> None:
        """Record the raw state; the first report counts at once, later flips after the dwell."""
        if entity_id not in self._occupied:
            return
        value = True if state == "on" else False if state == "off" else None
        first = self._occupied_since[entity_id] is None
        if first or (self._occupied[entity_id] is True) != (value is True):
            self._occupied_since[entity_id] = now
        if first:
            self._counted[entity_id] = value is True
        self._occupied[entity_id] = value
        self._settle_occupancy(now)

    def set_occupancy_dwell(self, seconds: float, now: float) -> None:
        self.occupancy_dwell = max(0.0, float(seconds))
        self._settle_occupancy(now)

    def _settle_occupancy(self, now: float) -> None:
        """Adopt a raw on/not-on value once it has held for the dwell time."""
        for entity_id, since in self._occupied_since.items():
            raw = self._occupied[entity_id] is True
            if since is not None and raw != self._counted[entity_id] and now - since >= self.occupancy_dwell:
                self._counted[entity_id] = raw

    def occupied(self, entity_id: str) -> bool:
        """Dwell-filtered occupancy used for room selection."""
        return self._counted.get(entity_id, False)

    # Derived state ------------------------------------------------------------
    def fresh(self, entity_id: str, now: float) -> bool:
        reported_at = self._reported_at.get(entity_id)
        return (reported_at is not None and self._values.get(entity_id) is not None
                and now - reported_at < self.timeout)

    def room_temperatures(self, now: float) -> dict[str, float | None]:
        return {room.name: _mean(self._values[s] for s in room.sensors if self.fresh(s, now))
                for room in self.rooms}

    def select_active(self, temperatures: dict[str, float | None]) -> tuple[str, ...]:
        """Occupied rooms with a temperature win; otherwise every room with a temperature."""
        occupied = tuple(room.name for room in self.rooms
                         if room.occupancy and self.occupied(room.occupancy)
                         and temperatures[room.name] is not None)
        if occupied:
            return occupied
        return tuple(room.name for room in self.rooms if temperatures[room.name] is not None)

    def blending(self, now: float) -> bool:
        return self._blend_started is not None and now < self._blend_started + self.transition

    @property
    def blend_until(self) -> float | None:
        return None if self._blend_started is None else self._blend_started + self.transition

    def update(self, now: float, *, hold: bool = False) -> bool:
        """Recompute the aggregate at ``now``; return True when the active-room set changed
        enough to matter for learning (a blend started or no value to blend from).

        ``hold`` keeps the current set (minus rooms without a fresh temperature) and
        stores the newly selected set in ``pending_rooms`` until a later unheld update.
        """
        self._settle_occupancy(now)
        temperatures = self.room_temperatures(now)
        selected = self.select_active(temperatures)
        active = selected
        if hold and self.active_rooms:
            kept = tuple(name for name in self.active_rooms if temperatures.get(name) is not None)
            if kept:
                active = kept
        self.pending_rooms = selected if set(selected) != set(active) else None
        target = _mean(temperatures[name] for name in active)
        changed = self.active_rooms is not None and set(active) != set(self.active_rooms)
        significant = False
        if changed:
            if (self.temperature is not None and target is not None
                    and abs(target - self.temperature) < self.min_delta):
                # A small step is applied directly: no blend, learning continues.
                self._blend_from = self._blend_started = None
            elif self.temperature is not None and target is not None:
                # Restart from what was shown last, also when a blend is still running.
                self._blend_from, self._blend_started = self.temperature, now
                significant = True
            else:
                self._blend_from = self._blend_started = None
                significant = True
        self.active_rooms = active
        if target is None:
            self._blend_from = self._blend_started = None
            self.temperature = None
        elif self.blending(now) and self._blend_from is not None:
            fraction = max(0.0, (now - self._blend_started) / self.transition)
            self.temperature = self._blend_from + (target - self._blend_from) * fraction
        else:
            self._blend_from = self._blend_started = None
            self.temperature = target
        return significant

    def next_deadline(self, now: float) -> float | None:
        """Earliest time the aggregate can change without a new report."""
        deadlines = [reported_at + self.timeout for entity, reported_at in self._reported_at.items()
                     if reported_at is not None and self.fresh(entity, now)]
        # A raw occupancy flip waiting for its dwell time.
        deadlines.extend(since + self.occupancy_dwell for entity, since in self._occupied_since.items()
                         if since is not None and (self._occupied[entity] is True) != self._counted[entity])
        if self.blending(now):
            deadlines.append(min(now + BLEND_TICK_SECONDS, self.blend_until))
        return min(deadlines) if deadlines else None
