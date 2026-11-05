"""Build planning intervals with an intact first action and UTC clock alignment."""
from datetime import datetime, timedelta

from solar_model.time_utils import utc_instant


def projection_intervals(starts_at: datetime, total_hours: int, visible_hours: int, minutes: tuple[int, ...]) -> list[tuple[datetime, datetime]]:
    """Keep the first 15 minutes intact and align subsequent conventional steps."""
    start = utc_instant(starts_at)
    end = start + timedelta(hours=total_hours)
    visible_end = min(end, start + timedelta(hours=visible_hours))
    cursor = start + timedelta(minutes=15)
    intervals = [(start, cursor)]

    def append_step(step: int) -> None:
        nonlocal cursor
        candidate = min(end, cursor + timedelta(minutes=step))
        # Sub-hour divisors align within the hour; longer whole-hour steps align to an hour.
        quantum = step if step < 60 and 60 % step == 0 else 60 if step % 60 == 0 else 0
        if quantum:
            aligned = candidate.replace(minute=(candidate.minute // quantum) * quantum, second=0, microsecond=0)
            if cursor < aligned < candidate:
                candidate = aligned
        if cursor < visible_end < candidate:
            intervals.append((cursor, visible_end))
            cursor = visible_end
        intervals.append((cursor, candidate))
        cursor = candidate

    for step in minutes[1:]:
        if cursor >= end:
            break
        append_step(step)
    while cursor < end:
        append_step(minutes[-1])
    return intervals
