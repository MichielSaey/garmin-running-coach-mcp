"""Closed workout step schema shared by create, combine, and schedule."""

from __future__ import annotations

from typing import Any, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hr_zones import zone_for_workout_type

_STEP_TYPES = {"warmup", "interval", "recovery", "cooldown"}


class EndTime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    minutes: float


class EndDistance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    meters: float


class TargetHr(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    zone: int = Field(ge=1, le=5)


class TargetPace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str
    mps_low: float
    mps_high: float


class TargetNone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str


class StepInput(BaseModel):
    """One release accepts the closed step model and the legacy fields."""

    model_config = ConfigDict(extra="forbid")
    type: str
    end: Optional[Union[EndTime, EndDistance]] = None
    target: Optional[Union[TargetHr, TargetPace, TargetNone, str]] = None
    estimated_pace_mps: Optional[float] = None
    duration_minutes: Optional[float] = None
    distance_meters: Optional[float] = None
    heart_rate_zone: Optional[int] = None
    workout_type: Optional[str] = None
    speed_mps_min: Optional[float] = None
    speed_mps_max: Optional[float] = None
    estimated_duration_minutes: Optional[float] = None
    iterations: Optional[int] = None
    steps: Optional[list["StepInput"]] = None


class SegmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    steps: Optional[list[StepInput]] = None


def _validation_error(exc: ValidationError) -> ValueError:
    extras = []
    for err in exc.errors():
        if err.get("type") == "extra_forbidden":
            loc = ".".join(str(part) for part in err.get("loc", []))
            extras.append(loc or "step")
    if extras:
        return ValueError(f"Unknown step keys: {', '.join(extras)}")
    return ValueError(str(exc))


def _dump_target(target: Any) -> Any:
    if isinstance(target, BaseModel):
        return target.model_dump()
    return target


def _normalize_one(raw: dict[str, Any], *, nested: bool) -> dict[str, Any]:
    try:
        parsed = StepInput.model_validate(raw)
    except ValidationError as exc:
        raise _validation_error(exc) from exc

    step_type = parsed.type.lower()
    if step_type == "repeat":
        if nested:
            raise ValueError("Repeat blocks cannot be nested.")
        if parsed.iterations is None or parsed.iterations < 1:
            raise ValueError("Repeat blocks require iterations >= 1.")
        if not parsed.steps:
            raise ValueError("Repeat blocks require a non-empty steps array.")
        return {
            "type": "repeat",
            "iterations": parsed.iterations,
            "steps": [_normalize_one(child.model_dump(), nested=True) for child in parsed.steps],
        }

    if step_type not in _STEP_TYPES:
        raise ValueError(
            f"Unsupported step type '{parsed.type}'. "
            f"Use one of: {', '.join(sorted(_STEP_TYPES | {'repeat'}))}."
        )

    end = _resolve_end(parsed)
    target = _resolve_target(parsed)
    canonical: dict[str, Any] = {"type": step_type, "end": end, "target": target}
    if parsed.estimated_pace_mps is not None:
        canonical["estimated_pace_mps"] = float(parsed.estimated_pace_mps)

    if end["type"] == "distance":
        has_pace = target["type"] == "pace" or parsed.estimated_pace_mps is not None
        if not has_pace:
            raise ValueError(
                "Distance steps require a pace target or estimated_pace_mps."
            )
    return canonical


def _resolve_end(parsed: StepInput) -> dict[str, Any]:
    legacy_time = parsed.duration_minutes is not None
    legacy_distance = parsed.distance_meters is not None
    if parsed.end is not None and (legacy_time or legacy_distance):
        raise ValueError("A step may set end or a legacy duration, not both.")
    if legacy_time and legacy_distance:
        raise ValueError("Each step may set duration_minutes or distance_meters, not both.")
    if parsed.end is not None:
        if parsed.end.type == "time":
            if float(parsed.end.minutes) <= 0:
                raise ValueError("Time end minutes must be positive.")
            return {"type": "time", "minutes": float(parsed.end.minutes)}
        if parsed.end.type == "distance":
            if float(parsed.end.meters) <= 0:
                raise ValueError("Distance end meters must be positive.")
            return {"type": "distance", "meters": float(parsed.end.meters)}
        raise ValueError("end.type must be 'time' or 'distance'.")
    if legacy_time:
        if float(parsed.duration_minutes) <= 0:
            raise ValueError("duration_minutes must be positive.")
        return {"type": "time", "minutes": float(parsed.duration_minutes)}
    if legacy_distance:
        if float(parsed.distance_meters) <= 0:
            raise ValueError("distance_meters must be positive.")
        return {"type": "distance", "meters": float(parsed.distance_meters)}
    raise ValueError("Each step requires an end (time or distance).")


def _resolve_target(parsed: StepInput) -> dict[str, Any]:
    explicit = _dump_target(parsed.target)
    legacy_zone = parsed.heart_rate_zone is not None
    legacy_speed = parsed.speed_mps_min is not None or parsed.speed_mps_max is not None
    if isinstance(explicit, dict) and (legacy_zone or legacy_speed):
        raise ValueError("A step may set target or legacy zone/speed fields, not both.")
    if isinstance(explicit, dict):
        kind = explicit.get("type")
        if kind == "hr_zone":
            zone = int(explicit["zone"])
            if zone < 1 or zone > 5:
                raise ValueError("Heart rate zone must be 1-5.")
            return {"type": "hr_zone", "zone": zone}
        if kind == "pace":
            low = float(explicit["mps_low"])
            high = float(explicit["mps_high"])
            if low <= 0 or high <= 0:
                raise ValueError("Pace targets must be positive m/s values.")
            if high < low:
                raise ValueError("mps_high must be >= mps_low.")
            return {"type": "pace", "mps_low": low, "mps_high": high}
        if kind == "none":
            return {"type": "none"}
        raise ValueError("target.type must be hr_zone, pace, or none.")
    if isinstance(explicit, str):
        label = explicit.lower()
        if label in {"speed", "pace"}:
            return _pace_from_legacy(parsed)
        if label == "none":
            return {"type": "none"}
        raise ValueError(f"Unknown target '{explicit}'.")
    if legacy_speed:
        return _pace_from_legacy(parsed)
    if legacy_zone:
        zone = int(parsed.heart_rate_zone)
        if zone < 1 or zone > 5:
            raise ValueError("Heart rate zone must be 1-5.")
        return {"type": "hr_zone", "zone": zone}
    if parsed.workout_type:
        return {"type": "hr_zone", "zone": zone_for_workout_type(parsed.workout_type)}
    return {"type": "hr_zone", "zone": zone_for_workout_type("base")}


def _pace_from_legacy(parsed: StepInput) -> dict[str, Any]:
    low = parsed.speed_mps_min
    high = parsed.speed_mps_max
    if low is None and high is None:
        raise ValueError("Speed target requires speed_mps_min and/or speed_mps_max.")
    if low is None:
        low = high
    if high is None:
        high = low
    low_f = float(low)
    high_f = float(high)
    if low_f <= 0 or high_f <= 0:
        raise ValueError("Speed targets must be positive m/s values.")
    if high_f < low_f:
        raise ValueError("speed_mps_max must be >= speed_mps_min.")
    return {"type": "pace", "mps_low": low_f, "mps_high": high_f}


def normalize_steps(steps: list[Any]) -> list[dict[str, Any]]:
    if not steps:
        raise ValueError("At least one workout step is required.")
    normalized = []
    for step in steps:
        if isinstance(step, StepInput):
            raw = step.model_dump()
        elif isinstance(step, dict):
            raw = step
        else:
            raise ValueError("Each workout step must be an object.")
        normalized.append(_normalize_one(raw, nested=False))
    return normalized


def canonical_to_builder(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Legacy builder dicts. Duration estimate is carried for distance steps."""
    built: list[dict[str, Any]] = []
    for step in steps:
        if step["type"] == "repeat":
            built.append(
                {
                    "type": "repeat",
                    "iterations": step["iterations"],
                    "steps": canonical_to_builder(step["steps"]),
                }
            )
            continue
        end = step["end"]
        target = step["target"]
        out: dict[str, Any] = {"type": step["type"]}
        if end["type"] == "time":
            out["duration_minutes"] = end["minutes"]
        else:
            out["distance_meters"] = end["meters"]
            pace = step.get("estimated_pace_mps")
            if target["type"] == "pace":
                pace = (float(target["mps_low"]) + float(target["mps_high"])) / 2.0
                out["target"] = "speed"
                out["speed_mps_min"] = target["mps_low"]
                out["speed_mps_max"] = target["mps_high"]
            if pace:
                out["estimated_pace_mps"] = float(pace)
                out["estimated_duration_minutes"] = float(end["meters"]) / float(pace) / 60.0
        if target["type"] == "hr_zone":
            out["heart_rate_zone"] = target["zone"]
        elif target["type"] == "none":
            out["target"] = "none"
        elif target["type"] == "pace" and end["type"] == "time":
            out["target"] = "speed"
            out["speed_mps_min"] = target["mps_low"]
            out["speed_mps_max"] = target["mps_high"]
        built.append(out)
    return built


def estimate_minutes(steps: list[dict[str, Any]]) -> float:
    from workout_builder import estimate_steps_duration_seconds

    seconds = estimate_steps_duration_seconds(canonical_to_builder(steps))
    return round(seconds / 60.0, 1)


def estimate_seconds(steps: list[dict[str, Any]]) -> int:
    from workout_builder import estimate_steps_duration_seconds

    return estimate_steps_duration_seconds(canonical_to_builder(steps))


def normalize_segments(segments: list[Any]) -> list[dict[str, Any]]:
    if not segments:
        raise ValueError("Provide at least one template segment to combine.")
    normalized = []
    for segment in segments:
        if isinstance(segment, SegmentInput):
            raw = segment.model_dump()
        elif isinstance(segment, dict):
            raw = segment
        else:
            raise ValueError("Each segment must be an object.")
        try:
            parsed = SegmentInput.model_validate(raw)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        item: dict[str, Any] = {}
        if parsed.template is not None:
            item["template"] = parsed.template
        if parsed.params is not None:
            item["params"] = parsed.params
        if parsed.steps is not None:
            item["steps"] = [step.model_dump() for step in parsed.steps]
        if "template" not in item and "steps" not in item:
            raise ValueError("Each segment needs 'template' or explicit 'steps'.")
        normalized.append(item)
    return normalized
