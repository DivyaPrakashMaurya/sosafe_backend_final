"""The safety schedule of a trip: total time and when the check-ins fall.

Exactly the arithmetic the app has always received (the numbers drive the
safety check-in timers on the phone), kept apart from the web code so it is
tested on its own, and so a cached Ola route is never mistaken for a cached
schedule: the buffer and the checkpoint count belong to each request.
"""

from __future__ import annotations

from typing import Any, Mapping


def route_response(raw: Mapping[str, Any], mode: str, added_buffer_mins: int, checkpoint_count: int) -> dict[str, Any]:
    """`raw` is Ola's part (`ola_raw_mins`, `distance_km`, `overview_polyline`)."""
    ola_raw_mins = int(raw["ola_raw_mins"])
    total_duration_mins = ola_raw_mins + added_buffer_mins

    divisor = checkpoint_count + 1
    safety_interval_mins = round(total_duration_mins / divisor, 2)
    trigger_schedule = [round(safety_interval_mins * i, 1) for i in range(1, checkpoint_count + 1)]

    return {
        "status": "success",
        "data": {
            "mode": mode,
            "ola_raw_mins": ola_raw_mins,
            "total_duration_mins": total_duration_mins,
            "distance_km": raw["distance_km"],
            "checkpoint_count": checkpoint_count,
            "safety_interval_mins": safety_interval_mins,
            "trigger_schedule": trigger_schedule,
            "overview_polyline": raw["overview_polyline"],
        },
    }
