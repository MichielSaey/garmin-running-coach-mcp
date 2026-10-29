"""schedule_week and sync_block. Idempotency lives in the plan store."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Optional

from calendar_events import (
    calendar_items,
    is_workout_item,
    item_date,
    schedule_id_from_item,
    tied_to_completed_activity,
    workout_id_from_item,
)
from plan_store import (
    delete_schedule_record,
    get_schedule_record,
    recorded_workout_ids_on_date,
    save_schedule_record,
)
from season_plan import (
    export_sessions,
    name_slug,
    resolve_sessions,
    schedule_warnings,
    week_monday,
)
from step_schema import canonical_to_builder, normalize_steps
from workout_builder import build_running_workout, extract_workout_id

CallOptional = Callable[..., Any]


def _ensure_key(session: dict[str, Any], week_start: str) -> dict[str, Any]:
    if session.get("idempotency_key"):
        return session
    copied = dict(session)
    copied["idempotency_key"] = (
        f"{week_start}|{session['date']}|{name_slug(str(session.get('name') or 'session'))}"
    )
    return copied


def _workouts_on_date(client: Any, call_optional: CallOptional, day: str) -> list[dict[str, Any]]:
    target = date.fromisoformat(day)
    rows = []
    for item in calendar_items(client, call_optional, target, target):
        if not is_workout_item(item):
            continue
        if item_date(item) != day:
            continue
        workout_id = workout_id_from_item(item)
        if workout_id is None:
            continue
        rows.append(item)
    return rows


def _schedule_id_from_upload(result: Any) -> Optional[int]:
    if not isinstance(result, dict):
        return None
    for key in ("workoutScheduleId", "scheduledWorkoutId", "scheduleId"):
        if result.get(key) is not None:
            return int(result[key])
    inner = result.get("schedule")
    if isinstance(inner, dict):
        return _schedule_id_from_upload(inner)
    return schedule_id_from_item(result)


def _unschedule_recorded(
    client: Any,
    call_optional: CallOptional,
    record: dict[str, Any],
    day: str,
) -> Optional[str]:
    """Unschedule a row this server recorded. Returns an error string, or None."""
    items = _workouts_on_date(client, call_optional, day)
    match = None
    for item in items:
        if workout_id_from_item(item) == int(record["workout_id"]):
            match = item
            break
    if match is not None and tied_to_completed_activity(match):
        return "recorded workout is tied to a completed activity"
    schedule_id = record.get("schedule_id")
    if schedule_id is None and match is not None:
        schedule_id = schedule_id_from_item(match)
    if schedule_id is None:
        return "recorded workout has no schedule id"
    result = call_optional(client, "unschedule_workout", int(schedule_id))
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    delete_schedule_record(record["idempotency_key"])
    return None


def _upload_session(
    client: Any,
    call_optional: CallOptional,
    session: dict[str, Any],
) -> dict[str, Any]:
    try:
        canonical = normalize_steps(session.get("steps") or [])
        workout = build_running_workout(
            str(session.get("name") or "Run"),
            canonical_to_builder(canonical),
            description=session.get("note"),
            sport=session.get("sport") or "running",
        )
    except ValueError as exc:
        return {"date": session.get("date"), "ok": False, "error": str(exc)}

    from garminconnect.workout import CyclingWorkout

    method = "upload_cycling_workout" if isinstance(workout, CyclingWorkout) else "upload_running_workout"
    uploaded = call_optional(client, method, workout)
    if not isinstance(uploaded, dict) or uploaded.get("error"):
        return {
            "date": session.get("date"),
            "ok": False,
            "error": (uploaded or {}).get("error") if isinstance(uploaded, dict) else "upload failed",
        }
    workout_id = extract_workout_id(uploaded) or uploaded.get("workoutId")
    if workout_id is None:
        return {"date": session.get("date"), "ok": False, "error": "upload did not return a workout id"}
    scheduled = call_optional(client, "schedule_workout", int(workout_id), session["date"])
    if isinstance(scheduled, dict) and scheduled.get("error"):
        save_schedule_record(
            idempotency_key=session["idempotency_key"],
            date=session["date"],
            workout_id=int(workout_id),
            schedule_id=None,
        )
        return {
            "date": session.get("date"),
            "ok": False,
            "workout_id": int(workout_id),
            "error": scheduled["error"],
        }
    schedule_id = _schedule_id_from_upload(scheduled)
    save_schedule_record(
        idempotency_key=session["idempotency_key"],
        date=session["date"],
        workout_id=int(workout_id),
        schedule_id=schedule_id,
    )
    return {"date": session["date"], "ok": True, "workout_id": int(workout_id)}


def _schedule_one(
    client: Any,
    call_optional: CallOptional,
    session: dict[str, Any],
    *,
    replace_existing: bool,
    real_today: date,
) -> dict[str, Any]:
    key = session["idempotency_key"]
    record = get_schedule_record(key)
    if record is not None and record.get("schedule_id") is None and record.get("workout_id"):
        scheduled = call_optional(
            client, "schedule_workout", int(record["workout_id"]), session["date"]
        )
        if isinstance(scheduled, dict) and scheduled.get("error"):
            return {
                "date": session["date"],
                "ok": False,
                "workout_id": int(record["workout_id"]),
                "error": scheduled["error"],
            }
        schedule_id = _schedule_id_from_upload(scheduled)
        save_schedule_record(
            idempotency_key=key,
            date=session["date"],
            workout_id=int(record["workout_id"]),
            schedule_id=schedule_id,
        )
        return {
            "date": session["date"],
            "ok": True,
            "workout_id": int(record["workout_id"]),
        }
    if record is not None:
        if not replace_existing or date.fromisoformat(session["date"]) < real_today:
            return {"date": session["date"], "ok": True, "skipped": "duplicate"}
        error = _unschedule_recorded(client, call_optional, record, session["date"])
        if error:
            return {"date": session["date"], "ok": False, "error": error}

    known_today = recorded_workout_ids_on_date(session["date"])
    on_date = _workouts_on_date(client, call_optional, session["date"])
    unrecorded = [
        item
        for item in on_date
        if workout_id_from_item(item) not in known_today
    ]
    if len(unrecorded) > 1:
        return {
            "date": session["date"],
            "ok": False,
            "warning": (
                f"Date {session['date']} has more than one unrecorded workout; "
                "not uploading or adopting."
            ),
        }
    if len(unrecorded) == 1:
        item = unrecorded[0]
        workout_id = int(workout_id_from_item(item))
        save_schedule_record(
            idempotency_key=key,
            date=session["date"],
            workout_id=workout_id,
            schedule_id=schedule_id_from_item(item),
        )
        return {"date": session["date"], "ok": True, "adopted": True, "workout_id": workout_id}

    if on_date:
        return {
            "date": session["date"],
            "ok": False,
            "warning": (
                f"Date {session['date']} already has a workout; not uploading another."
            ),
        }

    return _upload_session(client, call_optional, session)


def schedule_week(
    season: dict[str, Any],
    sessions: list[dict[str, Any]],
    *,
    client: Any,
    call_optional: CallOptional,
    week_start: str,
    dry_run: bool = False,
    replace_existing: bool = False,
    real_today: date | None = None,
    snapshot: dict[str, Any] | None = None,
    refresh_failed: bool = False,
) -> dict[str, Any]:
    phase_day = date.fromisoformat(week_start)
    clock = real_today or phase_day
    prepared = [_ensure_key(session, week_start) for session in sessions]
    warnings, allowed = schedule_warnings(
        season,
        prepared,
        today=phase_day,
        snapshot=snapshot,
        refresh_failed=refresh_failed,
    )
    allowed_keys = {session["idempotency_key"] for session in allowed}
    results: list[dict[str, Any]] = []
    for session in prepared:
        if session["idempotency_key"] not in allowed_keys:
            results.append(
                {"date": session["date"], "ok": False, "error": "unavailable_weekday"}
            )
            continue
        if dry_run:
            results.append({"date": session["date"], "ok": True, "dry_run": True})
            continue
        outcome = _schedule_one(
            client,
            call_optional,
            session,
            replace_existing=replace_existing,
            real_today=clock,
        )
        if outcome.get("warning"):
            warnings.append(str(outcome["warning"]))
        results.append(outcome)

    created = 0
    skipped = 0
    failed = 0
    for result in results:
        if result.get("skipped"):
            skipped += 1
        elif result.get("ok") and not result.get("dry_run"):
            created += 1
        elif not result.get("ok"):
            failed += 1
    return {
        "created": created,
        "skipped": skipped,
        "failed": failed,
        "warnings": warnings,
        "results": results,
    }


def sync_block(
    season: dict[str, Any],
    *,
    client: Any,
    call_optional: CallOptional,
    today: date,
    weeks_ahead: int = 4,
    replace_existing: bool = False,
    snapshot: dict[str, Any] | None = None,
    refresh_failed: bool = False,
) -> dict[str, Any]:
    weeks = max(1, min(int(weeks_ahead), 4))
    monday = week_monday(today)
    payloads = []
    warnings = []
    if not replace_existing:
        warnings.append("sync_block did not delete unrecorded calendar rows.")
    for offset in range(weeks):
        start = monday + timedelta(days=7 * offset)
        sessions = export_sessions(resolve_sessions(season, start))
        # Library ids stay off the export; policy warnings still see names.
        result = schedule_week(
            season,
            sessions,
            client=client,
            call_optional=call_optional,
            week_start=start.isoformat(),
            replace_existing=replace_existing,
            real_today=today,
            snapshot=snapshot,
            refresh_failed=refresh_failed,
        )
        warnings.extend(result.get("warnings") or [])
        payloads.append({"week_start": start.isoformat(), **result})
    return {"weeks": payloads, "warnings": warnings}
