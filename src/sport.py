"""Workout sport. Running is the default. Gravel is a cycling workout."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Sport(str, Enum):
    running = "running"
    gravel = "gravel"


_GARMIN_SPORT = {
    Sport.running: {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
    Sport.gravel: {"sportTypeId": 2, "sportTypeKey": "cycling", "displayOrder": 2},
}


def parse_sport(value: Sport | str | None = None) -> Sport:
    """Accept the enum or its value. Missing means running."""
    if value is None or value == "":
        return Sport.running
    if isinstance(value, Sport):
        return value
    try:
        return Sport(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError("unsupported_sport") from exc


def garmin_sport_type(sport: Sport | str | None = None) -> dict[str, Any]:
    return dict(_GARMIN_SPORT[parse_sport(sport)])


def upload_method(sport: Sport | str | None = None) -> str:
    """Garmin client method that accepts this sport's workout model."""
    if parse_sport(sport) is Sport.gravel:
        return "upload_cycling_workout"
    return "upload_running_workout"
