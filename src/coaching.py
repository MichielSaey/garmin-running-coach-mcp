"""Plan status and the coaching overview. Status never calls Garmin."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from calendar_events import events_between, scheduled_workouts_between
from plan_store import load_actuals, load_season, recent_notes, save_actuals
from season_plan import (
    adherence_counts,
    days_since_race,
    band_for_week,
    constraints_block,
    example_season,
    export_sessions,
    garmin_date_mismatch,
    goal_by_id,
    load_context_from_brief,
    resolve_sessions,
    scaling_block,
    session_policy,
    status_warnings,
    week_monday,
    what_you_can_do,
    where_we_are,
    zoned_now,
    zoned_today,
)
from zones import activity_date, activity_type_key, counts_toward_run_distance

CallOptional = Callable[..., Any]
DEFAULT_TIMEZONE = "Europe/Brussels"


def _present_where(where: dict[str, Any]) -> dict[str, Any]:
    payload = dict(where)
    payload.pop("active_goal_date_passed", None)
    return payload


def _parse_as_of(value: str, now: datetime) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def actuals_are_fresh(snapshot: dict[str, Any] | None, now: datetime) -> bool:
    if not snapshot or not snapshot.get("as_of"):
        return False
    as_of = _parse_as_of(str(snapshot["as_of"]), now)
    return now - as_of < timedelta(hours=24)


def summarize_activities(
    activities: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    """28-day window ending today, inclusive. Does not use the report window."""
    start = today - timedelta(days=27)
    run_km = 0.0
    longest = None
    runs_present = False
    other_min = 0.0
    other: list[dict[str, Any]] = []
    km_by_week: dict[str, float] = {}
    covered: list[str] = []

    monday = week_monday(start)
    while monday <= today:
        sunday = monday + timedelta(days=6)
        if monday >= start and sunday <= today:
            covered.append(monday.isoformat())
            km_by_week[monday.isoformat()] = 0.0
        monday += timedelta(days=7)

    for activity in activities:
        if not isinstance(activity, dict):
            continue
        act_day = activity_date(activity)
        if act_day is None or act_day < start or act_day > today:
            continue
        minutes = round((activity.get("movingDuration") or activity.get("duration") or 0) / 60, 1)
        if counts_toward_run_distance(activity):
            runs_present = True
            km = round((activity.get("distance") or 0) / 1000, 2)
            run_km += km
            if longest is None or km > longest:
                longest = km
            week = week_monday(act_day).isoformat()
            if week in km_by_week:
                km_by_week[week] = round(km_by_week[week] + km, 2)
            continue
        other_min += minutes
        other.append(
            {
                "date": act_day.isoformat(),
                "type": activity_type_key(activity),
                "name": activity.get("activityName") or activity.get("name"),
                "minutes": minutes,
            }
        )

    average = 0.0 if not runs_present else round(run_km / 4.0, 1)
    return {
        "last_4_week_avg_run_km": average,
        "longest_run_last_28_days_km": longest,
        "other_sport_min": round(other_min, 1),
        "other_activities": other,
        "run_km_by_week": km_by_week,
        "covered_weeks": covered,
        "runs_present": runs_present,
        "window": {"start": start.isoformat(), "end": today.isoformat()},
    }


def refresh_actuals(
    client: Any,
    call_optional: CallOptional,
    season: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    tz_name = (season or {}).get("timezone") or DEFAULT_TIMEZONE
    today = zoned_today(tz_name, now)
    start = today - timedelta(days=27)
    activities = call_optional(
        client, "get_activities_by_date", start.isoformat(), today.isoformat()
    )
    if isinstance(activities, dict) and activities.get("error"):
        raise RuntimeError(str(activities["error"]))
    if not isinstance(activities, list):
        raise RuntimeError("No activities returned from Garmin")
    summary = summarize_activities(activities, today)
    horizon = today + timedelta(days=400)
    summary["events"] = events_between(client, call_optional, today, horizon)
    summary["as_of"] = zoned_now(tz_name, now).isoformat(timespec="seconds")
    # get_activities rows do not carry a start date, so these stay null.
    summary["days_since_hard_session"] = None
    if season and season.get("season_id"):
        save_actuals(str(season["season_id"]), summary)
    return summary


def _recent_training(
    season: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    today: date,
) -> dict[str, Any]:
    if snapshot is None:
        adherence = (
            adherence_counts(season, None, today)
            if season
            else {
                "weeks_on_target": 0,
                "weeks_missed": 0,
                "weeks_over": 0,
                "weeks_unknown": 0,
                "evaluated_weeks": 0,
            }
        )
        return {
            "actuals_as_of": None,
            "last_4_week_avg_run_km": None,
            "longest_run_last_28_days_km": None,
            "days_since_race": days_since_race(season, today),
            "days_since_hard_session": None,
            "other_sport_min": None,
            "other_activities": [],
            "adherence": adherence,
        }
    return {
        "actuals_as_of": snapshot.get("as_of"),
        "last_4_week_avg_run_km": snapshot.get("last_4_week_avg_run_km"),
        "longest_run_last_28_days_km": snapshot.get("longest_run_last_28_days_km"),
        "days_since_race": days_since_race(season, today),
        "days_since_hard_session": None,
        "other_sport_min": snapshot.get("other_sport_min"),
        "other_activities": snapshot.get("other_activities") or [],
        "adherence": adherence_counts(season, snapshot, today) if season else {
            "weeks_on_target": 0,
            "weeks_missed": 0,
            "weeks_over": 0,
            "weeks_unknown": 0,
            "evaluated_weeks": 0,
        },
    }


def build_plan_status(
    season: dict[str, Any],
    *,
    now: datetime | None = None,
    snapshot: dict[str, Any] | None = None,
    notes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tz_name = season["timezone"]
    moment = zoned_now(tz_name, now)
    today = moment.date()
    if snapshot is None and season.get("season_id"):
        snapshot = load_actuals(str(season["season_id"]))
    if notes is None and season.get("season_id"):
        notes = recent_notes(str(season["season_id"]))
    notes = notes or []
    phase_day = today
    raw_sessions = resolve_sessions(season, phase_day)
    sessions = export_sessions(raw_sessions)
    where = where_we_are(season, today)
    band = band_for_week(season, today)
    scaling = scaling_block(band, sessions)
    policy = session_policy(season, today)
    stale = not actuals_are_fresh(snapshot, moment)
    warnings = status_warnings(
        season,
        today,
        snapshot,
        refresh_failed=stale,
        events=(snapshot or {}).get("events") if snapshot else [],
        sessions=sessions,
    )
    training = _recent_training(season, snapshot, today)
    presented = _present_where(where)
    return {
        "today": today.isoformat(),
        "timezone": tz_name,
        "where_we_are": presented,
        "constraints": constraints_block(season),
        "scaling": scaling,
        "sessions": sessions,
        "session_policy": policy,
        "recent_notes": notes,
        "recent_training": training,
        "warnings": warnings,
        "current_goal": presented.get("current_goal"),
        "next_goal": presented.get("next_goal"),
        "days_to_goal": presented.get("days_to_goal"),
        "current_phase": presented.get("phase"),
        "week_in_phase": presented.get("week_in_phase"),
        "phase_weeks_total": presented.get("phase_weeks_total"),
        "phase_weekly_km": scaling.get("phase_weekly_km"),
        "this_week_target": scaling.get("this_week_target_km"),
        "deload_week": scaling.get("deload_week"),
        "key_sessions_now": policy.get("now"),
        "key_sessions_later": policy.get("later"),
        "last_4_week_avg_run_km": training.get("last_4_week_avg_run_km"),
        "other_sport_min": training.get("other_sport_min"),
        "other_activities": training.get("other_activities"),
        "longest_run_last_28_days": training.get("longest_run_last_28_days_km"),
        "longest_run_last_28_days_km": training.get("longest_run_last_28_days_km"),
        "actuals_as_of": training.get("actuals_as_of"),
        "adherence": training.get("adherence"),
    }


def build_coaching_overview(
    client: Any,
    call_optional: CallOptional,
    *,
    season: dict[str, Any] | None,
    brief: dict[str, Any] | None,
    now: datetime | None = None,
    snapshot: dict[str, Any] | None = None,
    notes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    tz_name = (season or {}).get("timezone") or DEFAULT_TIMEZONE
    moment = zoned_now(tz_name, now)
    today = moment.date()
    refresh_failed = False
    if season and season.get("season_id") and snapshot is None:
        snapshot = load_actuals(str(season["season_id"]))
    if not actuals_are_fresh(snapshot, moment):
        try:
            snapshot = refresh_actuals(client, call_optional, season, moment)
        except Exception:
            refresh_failed = True
    if season is None:
        training = _recent_training(None, snapshot, today)
        week_end = today + timedelta(days=7)
        return {
            "today": today.isoformat(),
            "timezone": tz_name,
            "where_we_are": None,
            "constraints": None,
            "scaling": None,
            "sessions": [],
            "session_policy": {"now": [], "later": []},
            "recent_notes": [],
            "recent_training": training,
            "calendar": _calendar(client, call_optional, today, week_end),
            "warnings": ["no_season"],
            "load_context": load_context_from_brief(brief, hard_pct=None),
            "what_you_can_do": what_you_can_do(
                has_sessions=False,
                mismatch=None,
                goal_date=None,
                race_on_calendar=False,
            ),
        }

    if notes is None:
        notes = recent_notes(str(season["season_id"]))
    status = build_plan_status(season, now=moment, snapshot=snapshot, notes=notes)
    # Status treats a stale snapshot as refresh_failed for the volume rule.
    # When this call just failed a refresh, force that skip even if the old
    # snapshot is still inside 24h — it is not to be trusted for the delta.
    if refresh_failed:
        status["warnings"] = [item for item in status["warnings"] if "15%" not in item]
    week_end = today + timedelta(days=7)
    calendar = _calendar(client, call_optional, today, week_end)
    events = (snapshot or {}).get("events") or []
    # Prefer the live calendar race list when the snapshot has none yet.
    if not events:
        events = calendar["events"]
    mismatch = garmin_date_mismatch(season, events if isinstance(events, list) else [])
    warnings = list(status["warnings"])
    if mismatch and mismatch not in warnings:
        warnings.append(mismatch)
    if refresh_failed:
        warnings = [item for item in warnings if "15%" not in item]
    active = goal_by_id(season, season.get("active_goal_id"))
    goal_date = (active or {}).get("date")
    seen_events = [
        event
        for event in list(events) + list(calendar.get("events") or [])
        if isinstance(event, dict)
    ]
    titles = {str(event.get("title") or "") for event in seen_events}
    expected = (active or {}).get("garmin_event_title") or (active or {}).get("title")
    race_on_calendar = bool(expected) and any(expected in title or title in str(expected) for title in titles)
    hard_pct = (status.get("scaling") or {}).get("hard_pct")
    return {
        "today": today.isoformat(),
        "timezone": tz_name,
        "where_we_are": status["where_we_are"],
        "constraints": status["constraints"],
        "scaling": status["scaling"],
        "sessions": status["sessions"],
        "session_policy": status["session_policy"],
        "recent_notes": status["recent_notes"],
        "recent_training": status["recent_training"],
        "calendar": calendar,
        "warnings": warnings,
        "load_context": load_context_from_brief(brief, hard_pct=hard_pct),
        "what_you_can_do": what_you_can_do(
            has_sessions=bool(status["sessions"]),
            mismatch=mismatch,
            goal_date=goal_date,
            race_on_calendar=race_on_calendar or bool(mismatch),
        ),
    }


def _calendar(
    client: Any,
    call_optional: CallOptional,
    today: date,
    week_end: date,
) -> dict[str, Any]:
    try:
        workouts = scheduled_workouts_between(client, call_optional, today, week_end)
    except Exception:
        workouts = []
    try:
        events = events_between(client, call_optional, today, today + timedelta(days=400))
    except Exception:
        events = []
    return {"events": events, "scheduled_workouts": workouts}


def season_or_none() -> dict[str, Any] | None:
    return load_season()


def documented_season() -> dict[str, Any]:
    return example_season()
