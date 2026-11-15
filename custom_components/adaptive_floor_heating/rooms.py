"""Aggregate room sensors into one measured temperature; related: runtime.py, config_flow.py, const.py.

Pure logic on an injected monotonic clock. A room's temperature is the mean of
its fresh sensors; occupied rooms (occupancy ``on``) take precedence over all
rooms; a change of the active-room set blends linearly from the last displayed
value to the new aggregate over ``ROOM_TRANSITION_SECONDS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .const import (
    CONF_ROOM_NAME, CONF_ROOM_OCCUPANCY, CONF_ROOM_SENSORS, MAX_ROOM_SLOTS,
    ROOM_TRANSITION_SECONDS,
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
                 transition: float = ROOM_TRANSITION_SECONDS) -> None:
        self.rooms: tuple[Room, ...] = tuple(rooms)
        self.timeout = timeout
        self.transition = transition
        self.sensors = tuple(dict.fromkeys(s for room in self.rooms for s in room.sensors))
        self.occupancy_sensors = tuple(dict.fromkeys(
            room.occupancy for room in self.rooms if room.occupancy))
        self.entities = frozenset((*self.sensors, *self.occupancy_sensors))
        self._values: dict[str, float | None] = {entity: None for entity in self.sensors}
        self._reported_at: dict[str, float | None] = {entity: None for entity in self.sensors}
        self._occupied: dict[str, bool | None] = {entity: None for entity in self.occupancy_sensors}
        self.active_rooms: tuple[str, ...] | None = None
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

    def report_occupancy(self, entity_id: str, state: str | None) -> None:
        if entity_id in self._occupied:
            self._occupied[entity_id] = True if state == "on" else False if state == "off" else None

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
                         if room.occupancy and self._occupied.get(room.occupancy) is True
                         and temperatures[room.name] is not None)
        if occupied:
            return occupied
        return tuple(room.name for room in self.rooms if temperatures[room.name] is not None)

    def blending(self, now: float) -> bool:
        return self._blend_started is not None and now < self._blend_started + self.transition

    @property
    def blend_until(self) -> float | None:
        return None if self._blend_started is None else self._blend_started + self.transition

    def update(self, now: float) -> bool:
        """Recompute the aggregate at ``now``; return True when the active-room set changed."""
        temperatures = self.room_temperatures(now)
        active = self.select_active(temperatures)
        target = _mean(temperatures[name] for name in active)
        changed = self.active_rooms is not None and set(active) != set(self.active_rooms)
        if changed:
            # Restart from what was shown last, also when a blend is still running.
            if self.temperature is not None and target is not None:
                self._blend_from, self._blend_started = self.temperature, now
            else:
                self._blend_from = self._blend_started = None
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
        return changed

    def next_deadline(self, now: float) -> float | None:
        """Earliest time the aggregate can change without a new report."""
        deadlines = [reported_at + self.timeout for entity, reported_at in self._reported_at.items()
                     if reported_at is not None and self.fresh(entity, now)]
        if self.blending(now):
            deadlines.append(min(now + BLEND_TICK_SECONDS, self.blend_until))
        return min(deadlines) if deadlines else None
