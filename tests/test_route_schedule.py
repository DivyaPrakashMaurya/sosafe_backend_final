"""The check-in schedule must be exactly what the old backend returned: the phone's
safety timers run on these numbers."""

from __future__ import annotations

import itertools

from app.domain.route_schedule import route_response


def old_backend(ola_raw_mins: int, added_buffer_mins: int, checkpoint_count: int) -> dict:
    """The arithmetic of the original main.py, copied as it was."""
    total_duration_mins = ola_raw_mins + added_buffer_mins
    divisor = checkpoint_count + 1
    safety_interval_mins = round(total_duration_mins / divisor, 2)
    trigger_intervals = []
    for i in range(1, checkpoint_count + 1):
        trigger_intervals.append(round(safety_interval_mins * i, 1))
    return {
        "total_duration_mins": total_duration_mins,
        "safety_interval_mins": safety_interval_mins,
        "trigger_schedule": trigger_intervals,
    }


def test_same_numbers_as_the_old_backend_for_every_combination():
    for minutes, buffer, checkpoints in itertools.product(range(0, 181, 7), range(0, 61, 5), range(0, 21)):
        new = route_response({"ola_raw_mins": minutes, "distance_km": 1.0, "overview_polyline": "p"}, "walking", buffer, checkpoints)["data"]
        old = old_backend(minutes, buffer, checkpoints)
        assert (new["total_duration_mins"], new["safety_interval_mins"], new["trigger_schedule"]) == (
            old["total_duration_mins"],
            old["safety_interval_mins"],
            old["trigger_schedule"],
        ), (minutes, buffer, checkpoints)


def test_the_whole_answer():
    assert route_response({"ola_raw_mins": 20, "distance_km": 5.4, "overview_polyline": "abc"}, "driving", 10, 3) == {
        "status": "success",
        "data": {
            "mode": "driving",
            "ola_raw_mins": 20,
            "total_duration_mins": 30,
            "distance_km": 5.4,
            "checkpoint_count": 3,
            "safety_interval_mins": 7.5,
            "trigger_schedule": [7.5, 15.0, 22.5],
            "overview_polyline": "abc",
        },
    }
