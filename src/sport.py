"""Workout sport. Running is the default. Gravel is a cycling workout."""

from __future__ import annotations

from enum import Enum
from typing import Any


class Sport(str, Enum):
    running = "running"
    gravel = "gravel"


_GARMIN_SPORT = {
    Sport.running: {"sportTypeId": 1, "sportTypeKey": "running"},
    Sport.gravel: {"sportTypeId": 2, "sportTypeKey": "cycling"},
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
