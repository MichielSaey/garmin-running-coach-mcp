"""Named running sessions. Phases reference these by name."""

from __future__ import annotations

from typing import Any


def _time_step(step_type: str, minutes: float, zone: int) -> dict[str, Any]:
    return {
        "type": step_type,
        "end": {"type": "time", "minutes": minutes},
        "target": {"type": "hr_zone", "zone": zone},
    }


def _loop_rehearsal_steps() -> list[dict[str, Any]]:
    return [
        {
            "type": "repeat",
            "iterations": 5,
            "steps": [
                _time_step("interval", 45, 2),
                _time_step("recovery", 8, 1),
            ],
        }
    ]


def _threshold_steps() -> list[dict[str, Any]]:
    return [
        _time_step("warmup", 10, 2),
        {
            "type": "repeat",
            "iterations": 4,
            "steps": [
                _time_step("interval", 5, 4),
                _time_step("recovery", 2, 1),
            ],
        },
        _time_step("cooldown", 10, 1),
    ]


def _sprint_steps() -> list[dict[str, Any]]:
    return [
        _time_step("warmup", 15, 2),
        {
            "type": "repeat",
            "iterations": 6,
            "steps": [
                {
                    "type": "interval",
                    "end": {"type": "distance", "meters": 100},
                    "target": {"type": "pace", "mps_low": 5.5, "mps_high": 6.5},
                },
                _time_step("recovery", 1.5, 1),
            ],
        },
        _time_step("cooldown", 10, 1),
    ]


LIBRARY: dict[str, dict[str, Any]] = {
    "easy": {
        "intent": "easy",
        "type": "easy",
        "zone": 2,
        "minutes": 40,
        "steps": [_time_step("interval", 40, 2)],
    },
    "recovery": {
        "intent": "easy",
        "type": "recovery",
        "zone": 1,
        "minutes": 25,
        "steps": [_time_step("interval", 25, 1)],
    },
    "long": {
        "intent": "long",
        "type": "long",
        "zone": 2,
        "minutes": 90,
        "steps": [_time_step("interval", 90, 2)],
    },
    "threshold": {
        "intent": "hard",
        "type": "threshold",
        "zone": 4,
        "minutes": 48,
        "steps": _threshold_steps(),
    },
    "sprint": {
        "intent": "hard",
        "type": "sprint",
        "zone": 5,
        "minutes": 40,
        "steps": _sprint_steps(),
    },
    "loop_rehearsal": {
        "intent": "structure",
        "type": "loop",
        "zone": 2,
        "minutes": 265,
        "steps": _loop_rehearsal_steps(),
    },
    "transition_drill": {
        "intent": "structure",
        "type": "drill",
        "zone": 2,
        "minutes": 20,
        "steps": [_time_step("interval", 20, 2)],
    },
    "self_supported_long": {
        "intent": "long",
        "type": "long",
        "zone": 2,
        "minutes": 180,
        "steps": [_time_step("interval", 180, 2)],
    },
    "vert_repeats": {
        "intent": "hard",
        "type": "vert",
        "zone": 4,
        "minutes": 40,
        "steps": [
            {
                "type": "repeat",
                "iterations": 8,
                "steps": [
                    _time_step("interval", 3, 4),
                    _time_step("recovery", 2, 1),
                ],
            }
        ],
    },
    "back_to_back": {
        "intent": "long",
        "type": "long",
        "zone": 2,
        "minutes": 150,
        "steps": [
            _time_step("interval", 75, 2),
            _time_step("recovery", 10, 1),
            _time_step("interval", 65, 2),
        ],
    },
    "long_run": {
        "intent": "long",
        "type": "long",
        "zone": 2,
        "minutes": 90,
        "steps": [_time_step("interval", 90, 2)],
    },
    "dress_rehearsal_8_loops": {
        "intent": "structure",
        "type": "loop",
        "zone": 2,
        "minutes": 240,
        "steps": [
            {
                "type": "repeat",
                "iterations": 8,
                "steps": [
                    _time_step("interval", 25, 2),
                    _time_step("recovery", 5, 1),
                ],
            }
        ],
    },
}


def library_entry(name: str) -> dict[str, Any] | None:
    entry = LIBRARY.get(name)
    if entry is None:
        return None
    return {
        "name": name,
        "intent": entry["intent"],
        "type": entry["type"],
        "zone": entry["zone"],
        "minutes": entry["minutes"],
        "steps": entry["steps"],
    }
