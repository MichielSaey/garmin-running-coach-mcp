"""Build Garmin running workouts from step definitions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from garminconnect.workout import (
    ConditionType,
    ExecutableStep,
    RunningWorkout,
    StepType,
    TargetType,
    WorkoutSegment,
    create_repeat_group,
)

from hr_zones import heart_rate_zone_target, zone_for_workout_type

StepNode = Union[Dict[str, Any], Any]
VALID_STEP_TYPES = {"warmup", "interval", "recovery", "cooldown"}

STEP_TYPE_META = {
    "warmup": (StepType.WARMUP, "warmup", 1),
    "interval": (StepType.INTERVAL, "interval", 3),
    "recovery": (StepType.RECOVERY, "recovery", 4),
    "cooldown": (StepType.COOLDOWN, "cooldown", 2),
}


def _step_factory(step_type: str):
    if step_type not in STEP_TYPE_META:
        raise ValueError(f"Unsupported step type '{step_type}'.")
    return STEP_TYPE_META[step_type]


def _resolve_zone(step: Dict[str, Any], default_zone: Optional[int]) -> int:
    if "heart_rate_zone" in step and step["heart_rate_zone"] is not None:
        return int(step["heart_rate_zone"])
    if "workout_type" in step and step["workout_type"]:
        return zone_for_workout_type(str(step["workout_type"]))
    if default_zone is not None:
        return default_zone
    return zone_for_workout_type("base")


def _minutes_to_seconds(minutes: float) -> float:
    return float(minutes) * 60.0


def _no_target() -> dict[str, Any]:
    return {
        "workoutTargetTypeId": TargetType.NO_TARGET,
        "workoutTargetTypeKey": "no.target",
        "displayOrder": 1,
    }


def _speed_zone_target() -> dict[str, Any]:
    return {
        "workoutTargetTypeId": TargetType.SPEED_ZONE,
        "workoutTargetTypeKey": "speed.zone",
        "displayOrder": 5,
    }


def _time_end_condition() -> dict[str, Any]:
    return {
        "conditionTypeId": ConditionType.TIME,
        "conditionTypeKey": "time",
        "displayOrder": 2,
        "displayable": True,
    }


def _distance_end_condition() -> dict[str, Any]:
    return {
        "conditionTypeId": ConditionType.DISTANCE,
        "conditionTypeKey": "distance",
        "displayOrder": 3,
        "displayable": True,
    }


def _uses_speed_target(step: Dict[str, Any]) -> bool:
    target = str(step.get("target") or "").lower()
    if target in {"speed", "pace"}:
        return True
    return step.get("speed_mps_min") is not None or step.get("speed_mps_max") is not None


def _speed_bounds(step: Dict[str, Any]) -> tuple[float, float]:
    speed_min = step.get("speed_mps_min")
    speed_max = step.get("speed_mps_max")
    if speed_min is None and speed_max is None:
        raise ValueError("Speed target requires speed_mps_min and/or speed_mps_max.")
    if speed_min is None:
        speed_min = speed_max
    if speed_max is None:
        speed_max = speed_min
    low = float(speed_min)
    high = float(speed_max)
    if low <= 0 or high <= 0:
        raise ValueError("Speed targets must be positive m/s values.")
    if high < low:
        raise ValueError("speed_mps_max must be >= speed_mps_min.")
    return low, high


def _end_block(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    end = step.get("end")
    return end if isinstance(end, dict) else None


def _pace_midpoint_mps(step: Dict[str, Any]) -> Optional[float]:
    if step.get("estimated_pace_mps") not in (None, ""):
        pace = float(step["estimated_pace_mps"])
        return pace if pace > 0 else None
    target = step.get("target")
    if isinstance(target, dict) and target.get("type") == "pace":
        low = float(target["mps_low"])
        high = float(target["mps_high"])
        mid = (low + high) / 2.0
        return mid if mid > 0 else None
    if _uses_speed_target(step):
        low, high = _speed_bounds(step)
        mid = (low + high) / 2.0
        return mid if mid > 0 else None
    return None


def _step_estimated_seconds(step: Dict[str, Any]) -> float:
    """Estimate wall-clock seconds for planning/nutrition (not Garmin endCondition)."""
    if step.get("estimated_duration_minutes") is not None:
        return float(step["estimated_duration_minutes"]) * 60.0
    end = _end_block(step)
    if end is not None and end.get("type") == "time":
        return float(end["minutes"]) * 60.0
    if end is not None and end.get("type") == "distance":
        pace = _pace_midpoint_mps(step)
        if pace:
            return float(end["meters"]) / pace
    if step.get("duration_minutes") is not None:
        return float(step["duration_minutes"]) * 60.0
    distance = step.get("distance_meters")
    if distance is not None:
        pace = _pace_midpoint_mps(step)
        if pace:
            return float(distance) / pace
    return 0.0


def _build_cue_step(step: Dict[str, Any], step_order: int):
    message = step.get("message") or step.get("description")
    if not message:
        raise ValueError("Cue steps require message or description.")

    return ExecutableStep(
        stepOrder=step_order,
        stepType={
            "stepTypeId": StepType.OTHER,
            "stepTypeKey": "other",
            "displayOrder": 7,
        },
        endCondition={
            "conditionTypeId": ConditionType.LAP_BUTTON,
            "conditionTypeKey": "lap.button",
            "displayOrder": 1,
            "displayable": True,
        },
        targetType=_no_target(),
        description=str(message),
    )


def _build_executable_step(
    step: Dict[str, Any],
    step_order: int,
    default_zone: Optional[int] = None,
):
    step_type = str(step.get("type", "interval")).lower()
    if step_type == "repeat":
        raise ValueError("Repeat blocks must use type='repeat' at the top level.")

    if step_type not in VALID_STEP_TYPES:
        raise ValueError(
            f"Unsupported step type '{step_type}'. "
            f"Use one of: {', '.join(sorted(VALID_STEP_TYPES))}."
        )

    end = _end_block(step)
    duration_minutes = step.get("duration_minutes")
    distance_meters = step.get("distance_meters")
    if end is not None:
        if end.get("type") == "time":
            duration_minutes = end.get("minutes")
            distance_meters = None
        elif end.get("type") == "distance":
            distance_meters = end.get("meters")
            duration_minutes = None
    if duration_minutes is not None and distance_meters is not None:
        raise ValueError(
            "Each step may set duration_minutes or distance_meters, not both."
        )
    if duration_minutes is None and distance_meters is None:
        raise ValueError("Each step requires duration_minutes or distance_meters.")

    step_type_id, step_type_key, display_order = _step_factory(step_type)
    common = {
        "stepOrder": step_order,
        "stepType": {
            "stepTypeId": step_type_id,
            "stepTypeKey": step_type_key,
            "displayOrder": display_order,
        },
    }

    if distance_meters is not None:
        meters = float(distance_meters)
        if meters <= 0:
            raise ValueError("distance_meters must be positive.")
        if _pace_midpoint_mps(step) is None:
            raise ValueError(
                "distance_meters steps require a speed target "
                "(target='speed' with speed_mps_min / speed_mps_max)."
            )
        end_condition = _distance_end_condition()
        end_value = meters
    else:
        end_condition = _time_end_condition()
        end_value = _minutes_to_seconds(float(duration_minutes))

    target_label = step.get("target")
    if isinstance(target_label, str) and target_label.lower() == "none":
        return ExecutableStep(
            **common,
            endCondition=end_condition,
            endConditionValue=end_value,
            targetType=_no_target(),
        )

    if _uses_speed_target(step):
        speed_min, speed_max = _speed_bounds(step)
        return ExecutableStep(
            **common,
            endCondition=end_condition,
            endConditionValue=end_value,
            targetType=_speed_zone_target(),
            targetValueOne=speed_min,
            targetValueTwo=speed_max,
        )

    zone = _resolve_zone(step, default_zone)
    return ExecutableStep(
        **common,
        endCondition=end_condition,
        endConditionValue=end_value,
        targetType=heart_rate_zone_target(),
        zoneNumber=zone,
    )


def _build_repeat_group(step: Dict[str, Any], step_order: int, default_zone: Optional[int]):
    iterations = step.get("iterations")
    nested_steps = step.get("steps")
    if not isinstance(iterations, int) or iterations < 1:
        raise ValueError("Repeat blocks require iterations >= 1.")
    if not isinstance(nested_steps, list) or not nested_steps:
        raise ValueError("Repeat blocks require a non-empty steps array.")

    inner_steps = []
    for index, nested in enumerate(nested_steps, start=1):
        if not isinstance(nested, dict):
            raise ValueError("Repeat block steps must be objects.")
        inner_steps.append(_build_executable_step(nested, index, default_zone))

    return create_repeat_group(iterations, inner_steps, step_order)


def build_workout_steps(
    steps: List[Dict[str, Any]],
    default_zone: Optional[int] = None,
) -> List[Any]:
    if not steps:
        raise ValueError("At least one workout step is required.")

    built_steps = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError("Each workout step must be an object.")
        step_type = str(step.get("type", "interval")).lower()
        if step_type == "repeat":
            built_steps.append(_build_repeat_group(step, index, default_zone))
        elif step_type == "cue":
            built_steps.append(_build_cue_step(step, index))
        else:
            built_steps.append(_build_executable_step(step, index, default_zone))
    return built_steps


def estimate_steps_duration_seconds(steps: List[Dict[str, Any]]) -> int:
    total = 0.0
    for step in steps:
        step_type = str(step.get("type", "interval")).lower()
        if step_type == "cue":
            continue
        if step_type == "repeat":
            iterations = int(step.get("iterations", 1))
            nested_steps = step.get("steps") or []
            for nested in nested_steps:
                total += _step_estimated_seconds(nested) * iterations
            continue
        total += _step_estimated_seconds(step)
    return max(int(total), 60)


def _executable_step_seconds(data: Dict[str, Any]) -> float:
    """Seconds contributed by a built ExecutableStepDTO (time or distance+speed)."""
    if data.get("stepType", {}).get("stepTypeKey") == "other":
        return 0.0
    condition = data.get("endCondition") or {}
    condition_key = condition.get("conditionTypeKey") or ""
    value = float(data.get("endConditionValue") or 0)
    if condition_key == "distance":
        speed_one = data.get("targetValueOne")
        speed_two = data.get("targetValueTwo")
        speeds = [float(s) for s in (speed_one, speed_two) if s is not None]
        if speeds:
            mid = sum(speeds) / len(speeds)
            if mid > 0:
                return value / mid
        return 0.0
    if condition_key == "lap.button":
        return 0.0
    return value


def estimate_duration_seconds(steps: List[Any]) -> int:
    total = 0.0

    def walk(node: Any) -> None:
        nonlocal total
        if isinstance(node, dict):
            if node.get("type") == "RepeatGroupDTO" or "numberOfIterations" in node:
                for child in node.get("workoutSteps", []):
                    for _ in range(int(node.get("numberOfIterations", 1))):
                        walk(child)
                return
            total += _executable_step_seconds(node)
            return

        data = node.model_dump() if hasattr(node, "model_dump") else {}
        if data.get("type") == "RepeatGroupDTO":
            for child in data.get("workoutSteps", []):
                for _ in range(int(data.get("numberOfIterations", 1))):
                    walk(child)
            return
        total += _executable_step_seconds(data)

    for step in steps:
        walk(step)
    return max(int(total), 60)


def build_running_workout(
    name: str,
    steps: List[Dict[str, Any]],
    description: Optional[str] = None,
    default_zone: Optional[int] = None,
    sport: Optional[str] = None,
) -> RunningWorkout:
    from sport import garmin_sport_type

    workout_steps = build_workout_steps(steps, default_zone=default_zone)
    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=garmin_sport_type(sport),
        workoutSteps=workout_steps,
    )
    return RunningWorkout(
        workoutName=name,
        estimatedDurationInSecs=estimate_steps_duration_seconds(steps),
        workoutSegments=[segment],
        description=description,
    )


def extract_workout_id(upload_result: Dict[str, Any]) -> Optional[int]:
    if not isinstance(upload_result, dict):
        return None
    for key in ("workoutId", "id"):
        value = upload_result.get(key)
        if value is not None:
            return int(value)
    workout = upload_result.get("workout")
    if isinstance(workout, dict) and workout.get("workoutId") is not None:
        return int(workout["workoutId"])
    return None
