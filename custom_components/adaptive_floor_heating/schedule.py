"""Pure daily heating schedule slots and learned pre-heat lead; related: runtime.py, config_flow.py, switch.py."""

from dataclasses import dataclass
from datetime import date, datetime, time as ClockTime, timedelta
from math import isfinite
from typing import Any

from .const import MAX_TARGET, MIN_TARGET

SCHEDULE_DAYS = ("all", "weekday", "weekend")
MAX_SCHEDULE_SLOTS = 12
# Never start a scheduled raise earlier than this, whatever the learned lead says.
MAX_PREHEAT_LEAD_MINUTES = 120
# Learned runtime samples required before an early start or an early stop may use the schedule.
MIN_SCHEDULE_RUNTIME_SAMPLES = 3


@dataclass(frozen=True)
class ScheduleSlot:
    """One local wall-clock target change for a day type."""

    time: ClockTime
    temperature: float
    days: str = "all"

    def applies(self, day: date) -> bool:
        weekend = day.weekday() >= 5
        return self.days == "all" or (self.days == "weekend") == weekend

    def as_dict(self) -> dict[str, Any]:
        return {"time": self.time.strftime("%H:%M"), "temperature": self.temperature, "days": self.days}


@dataclass(frozen=True)
class SlotOccurrence:
    """A slot resolved to a concrete local datetime."""

    slot: ScheduleSlot
    at: datetime


def _parse_time(value: Any) -> ClockTime:
    if not isinstance(value, str) or len(value.strip()) not in (4, 5) or ":" not in value:
        raise ValueError("schedule_invalid_time")
    hour, _, minute = value.strip().partition(":")
    if not (hour.isdigit() and minute.isdigit() and len(minute) == 2 and 0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError("schedule_invalid_time")
    return ClockTime(int(hour), int(minute))


def make_slot(when: Any, temperature: Any, days: Any = "all", *,
              low: float = MIN_TARGET, high: float = MAX_TARGET) -> ScheduleSlot:
    """Validate one slot; ValueError carries a translation error key."""
    clock = when.replace(second=0, microsecond=0, tzinfo=None) if isinstance(when, ClockTime) else _parse_time(when)
    try:
        value = float(temperature) if not isinstance(temperature, bool) else None
    except (TypeError, ValueError):
        value = None
    if value is None or not isfinite(value) or not low <= value <= high:
        raise ValueError("schedule_invalid_temperature")
    days = (days or "all").strip().lower() if isinstance(days, str) or days is None else None
    if days not in SCHEDULE_DAYS:
        raise ValueError("schedule_invalid_days")
    return ScheduleSlot(clock, round(value, 1), days)


def _validate_set(slots: list[ScheduleSlot]) -> tuple[ScheduleSlot, ...]:
    if len(slots) > MAX_SCHEDULE_SLOTS:
        raise ValueError("schedule_too_many")
    for index, slot in enumerate(slots):
        for other in slots[index + 1:]:
            # The same minute on overlapping day types would make the active target ambiguous.
            if slot.time == other.time and (slot.days == other.days or "all" in (slot.days, other.days)):
                raise ValueError("schedule_duplicate_time")
    return tuple(sorted(slots, key=lambda item: (item.time, SCHEDULE_DAYS.index(item.days))))


def parse_slots(text: Any, **limits) -> tuple[ScheduleSlot, ...]:
    """Parse one 'HH:MM temperature [all|weekday|weekend]' slot per line or ';'."""
    if text is None:
        return ()
    if not isinstance(text, str):
        raise ValueError("schedule_invalid_format")
    slots = []
    for line in text.replace(";", "\n").splitlines():
        parts = line.replace(",", " ").split()
        if not parts:
            continue
        if len(parts) not in (2, 3):
            raise ValueError("schedule_invalid_format")
        slots.append(make_slot(parts[0], parts[1], parts[2] if len(parts) == 3 else "all", **limits))
    return _validate_set(slots)


def format_slots(slots) -> str:
    return "\n".join(f"{slot.time.strftime('%H:%M')} {slot.temperature:g} {slot.days}" for slot in slots)


def encode_slots(slots) -> list[dict[str, Any]]:
    return [slot.as_dict() for slot in slots]


def decode_slots(data: Any) -> tuple[ScheduleSlot, ...]:
    """Restore stored option rows; raise ValueError on any malformed row."""
    if data is None:
        return ()
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError("schedule_invalid_format")
    return _validate_set([make_slot(row.get("time"), row.get("temperature"), row.get("days", "all")) for row in data])


def _occurrences(slots, day: date, zone):
    return [SlotOccurrence(slot, datetime.combine(day, slot.time, zone)) for slot in slots if slot.applies(day)]


def active_slot(slots, now: datetime) -> SlotOccurrence | None:
    """Most recent slot at or before ``now`` for its day type, wrapping to earlier days."""
    for back in range(8):
        day = now.date() - timedelta(days=back)
        found = [item for item in _occurrences(slots, day, now.tzinfo) if item.at <= now]
        if found:
            return max(found, key=lambda item: item.at)
    return None


def next_slot(slots, after: datetime) -> SlotOccurrence | None:
    """First slot strictly after ``after``, wrapping past midnight and weekends."""
    for ahead in range(9):
        day = after.date() + timedelta(days=ahead)
        found = [item for item in _occurrences(slots, day, after.tzinfo) if item.at > after]
        if found:
            return min(found, key=lambda item: item.at)
    return None


def preheat_lead_minutes(model, start: float | None, target: float, outdoor: float | None) -> float | None:
    """Learned heater runtime to lift ``start`` to ``target``, or None without measured support.

    Reuses the runtime->eventual-Peak curve that AUTO predictive OFF inverts (curve_learning.py).
    Requires enough samples, two supported shortest runs, and a rise inside the measured range;
    the lead is then capped at MAX_PREHEAT_LEAD_MINUTES.
    """
    if (model is None or start is None or not isfinite(start) or target <= start
            or len(model.runtime_samples) < MIN_SCHEDULE_RUNTIME_SAMPLES
            or model.runtime_control_minimum(outdoor) is None):
        return None
    points = model._runtime_curve(outdoor)
    if len(points) < 2 or target - start > points[-1][1]:
        return None
    required = model.required_heating_minutes(start, target, outdoor, points=points)
    if required is None or not isfinite(required) or required > points[-1][0]:
        return None
    return min(float(MAX_PREHEAT_LEAD_MINUTES), max(0.0, required))
