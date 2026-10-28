"""Season working model: bands, sessions, status. Not a frozen API."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from session_library import library_entry

WEEKDAYS = {
    "MONDAY": 0,
    "TUESDAY": 1,
    "WEDNESDAY": 2,
    "THURSDAY": 3,
    "FRIDAY": 4,
    "SATURDAY": 5,
    "SUNDAY": 6,
}

# Easy-pace estimates so a Z1/Z2 week can be checked against the kilometre band.
ZONE_KMH = {1: 9.2, 2: 9.3, 3: 10.5, 4: 12.0, 5: 14.0}

LOAD_LABEL = "Recent load only. sessions and scaling win if this disagrees."


def zoned_now(tz_name: str, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(tz_name)
    moment = now or datetime.now(zone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def zoned_today(tz_name: str, now: datetime | None = None) -> date:
    return zoned_now(tz_name, now).date()


def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _parse_date(value: str) -> date:
    return date.fromisoformat(value[:10])


def _num(value: float) -> int | float:
    rounded = round(float(value), 1)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def name_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "session"


def _phases(season: dict[str, Any]) -> list[dict[str, Any]]:
    phases = list(season.get("phases") or [])
    return sorted(phases, key=lambda phase: phase.get("start") or "")


def phase_bounds(season: dict[str, Any], phase: dict[str, Any]) -> tuple[date, date]:
    phases = _phases(season)
    start = _parse_date(phase["start"])
    if phase.get("end"):
        return start, _parse_date(phase["end"])
    later = [item for item in phases if item.get("start") and _parse_date(item["start"]) > start]
    if later:
        return start, _parse_date(later[0]["start"]) - timedelta(days=1)
    return start, start


def phase_weeks_total(season: dict[str, Any], phase: dict[str, Any]) -> int:
    start, end = phase_bounds(season, phase)
    return ((week_monday(end) - week_monday(start)).days // 7) + 1


def week_in_phase(phase: dict[str, Any], day: date) -> int:
    start = _parse_date(phase["start"])
    return ((week_monday(day) - week_monday(start)).days // 7) + 1


def current_phase(season: dict[str, Any], today: date) -> dict[str, Any] | None:
    for phase in _phases(season):
        start, end = phase_bounds(season, phase)
        if start <= today <= end:
            return phase
    return None


def _goals(season: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(season.get("goals") or [], key=lambda goal: goal.get("date") or "")


def goal_by_id(season: dict[str, Any], goal_id: str | None) -> dict[str, Any] | None:
    if not goal_id:
        return None
    for goal in _goals(season):
        if goal.get("goal_id") == goal_id:
            return goal
    return None


def next_goal_after(season: dict[str, Any], goal: dict[str, Any] | None) -> dict[str, Any] | None:
    goals = _goals(season)
    if goal is None:
        return goals[0] if goals else None
    goal_date = goal.get("date") or ""
    later = [item for item in goals if (item.get("date") or "") > goal_date]
    return later[0] if later else None


def _public_goal(goal: dict[str, Any] | None, *, today: date | None = None) -> dict[str, Any] | None:
    if goal is None:
        return None
    payload = {
        "goal_id": goal.get("goal_id"),
        "title": goal.get("title"),
        "date": goal.get("date"),
        "priority": goal.get("priority"),
    }
    if goal.get("type") is not None:
        payload["type"] = goal.get("type")
    if "min_longest_run_km" in goal:
        payload["min_longest_run_km"] = goal.get("min_longest_run_km")
    if today is not None and goal.get("date"):
        payload["days_to_goal"] = (_parse_date(goal["date"]) - today).days
    return payload


def apply_two_week_step(
    band: list[float] | None, weeks_total: int, week_index: int
) -> list[Any] | None:
    """Week 1 of a 2-week phase steps the high end halfway. Longer phases do not."""
    if not band or weeks_total != 2 or week_index != 1:
        return [_num(band[0]), _num(band[1])] if band else None
    low = float(band[0])
    high = float(band[1])
    stepped = low + (high - low) / 2.0
    shown = int(math.floor(stepped + 0.5))
    return [_num(low), shown]


def band_for_week(season: dict[str, Any], week_monday_date: date) -> dict[str, Any]:
    """Kilometre band for the Monday that starts ``week_monday_date``'s week."""
    monday = week_monday(week_monday_date)
    phase = None
    sunday = monday + timedelta(days=6)
    for item in _phases(season):
        start, end = phase_bounds(season, item)
        if monday <= end and start <= sunday:
            phase = item
            break
    if phase is None:
        phase = current_phase(season, monday)
    override = None
    for entry in season.get("week_overrides") or []:
        if entry.get("week_of") == monday.isoformat():
            override = entry
            break

    empty = {
        "phase": phase,
        "phase_weekly_km": None,
        "this_week_target_km": None,
        "overridden": False,
        "override_reason": None,
        "hard_pct": None,
        "deload_week": False,
        "missing_week_band": phase is not None and not (phase or {}).get("weekly_km"),
    }
    if phase is None:
        empty["missing_week_band"] = False
        return empty

    phase_band = phase.get("weekly_km")
    hard_pct = phase.get("hard_pct")
    weeks_total = phase_weeks_total(season, phase)
    index = week_in_phase(phase, monday)
    deload_week = False
    if phase.get("deload_every_n_weeks"):
        every = int(phase["deload_every_n_weeks"])
        if every > 0 and index % every == 0:
            deload_week = True

    if override and override.get("weekly_km"):
        band = [_num(override["weekly_km"][0]), _num(override["weekly_km"][1])]
        if override.get("hard_pct") is not None:
            hard_pct = override["hard_pct"]
        return {
            "phase": phase,
            "phase_weekly_km": [_num(phase_band[0]), _num(phase_band[1])] if phase_band else None,
            "this_week_target_km": band,
            "overridden": True,
            "override_reason": override.get("reason"),
            "hard_pct": hard_pct,
            "deload_week": False,
            "missing_week_band": False,
            "override_sessions": override.get("sessions"),
        }

    if not phase_band:
        return {
            "phase": phase,
            "phase_weekly_km": None,
            "this_week_target_km": None,
            "overridden": False,
            "override_reason": None,
            "hard_pct": hard_pct,
            "deload_week": deload_week,
            "missing_week_band": True,
        }

    band = apply_two_week_step(list(phase_band), weeks_total, index)
    if deload_week:
        factor = phase.get("deload_factor")
        if factor is None:
            raise ValueError("deload_factor is required when deload_every_n_weeks is set.")
        band = [_num(float(band[0]) * float(factor)), _num(float(band[1]) * float(factor))]
    return {
        "phase": phase,
        "phase_weekly_km": [_num(phase_band[0]), _num(phase_band[1])],
        "this_week_target_km": band,
        "overridden": False,
        "override_reason": None,
        "hard_pct": hard_pct,
        "deload_week": deload_week,
        "missing_week_band": False,
    }


def _estimate_km(minutes: float, zone: int | None) -> float:
    pace = ZONE_KMH.get(int(zone or 2), ZONE_KMH[2])
    return round(float(minutes) / 60.0 * pace, 1)


def _default_template(phase: dict[str, Any]) -> list[dict[str, Any]]:
    del phase
    return [
        {"weekday": "TUESDAY", "library": "easy", "minutes": 45, "zone": 2},
        {"weekday": "THURSDAY", "library": "easy", "minutes": 45, "zone": 2},
        {"weekday": "SUNDAY", "library": "long", "minutes": 90, "zone": 2},
    ]


def _materialize_session(
    spec: dict[str, Any],
    day: date,
    week_start: date,
) -> dict[str, Any]:
    library_name = spec.get("library") or spec.get("session")
    entry = library_entry(library_name) if library_name else None
    minutes = spec.get("minutes", entry["minutes"] if entry else 30)
    zone = spec.get("zone", entry["zone"] if entry else 2)
    intent = spec.get("intent", entry["intent"] if entry else "easy")
    session_type = spec.get("type", entry["type"] if entry else "easy")
    steps = spec.get("steps") or (entry["steps"] if entry else None)
    if steps is None:
        steps = [
            {
                "type": "interval",
                "end": {"type": "time", "minutes": minutes},
                "target": {"type": "hr_zone", "zone": zone},
            }
        ]
    if spec.get("minutes") is not None or spec.get("zone") is not None:
        if not spec.get("steps") and (entry is None or spec.get("minutes") or spec.get("zone")):
            # Explicit minutes/zone on a template row replace the library duration.
            if spec.get("minutes") is not None or spec.get("zone") is not None:
                if not spec.get("steps"):
                    steps = [
                        {
                            "type": "interval",
                            "end": {"type": "time", "minutes": minutes},
                            "target": {"type": "hr_zone", "zone": zone},
                        }
                    ]
    name = spec.get("name")
    if not name:
        short = day.strftime("%a")
        label = (library_name or session_type or "run").replace("_", " ").title()
        name = f"{short} {label}"
    return {
        "date": day.isoformat(),
        "type": session_type,
        "intent": intent,
        "name": name,
        "minutes": minutes,
        "zone": zone,
        "distance_km_estimate": _estimate_km(float(minutes), zone),
        "note": spec.get("note"),
        "library": library_name,
        "idempotency_key": f"{week_start.isoformat()}|{day.isoformat()}|{name_slug(name)}",
        "steps": steps,
    }


def resolve_sessions(
    season: dict[str, Any],
    week_monday_date: date,
    *,
    band: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    monday = week_monday(week_monday_date)
    scaling = band or band_for_week(season, monday)
    phase = scaling.get("phase")
    if phase is None:
        return []
    start, end = phase_bounds(season, phase)
    if scaling.get("override_sessions"):
        template = scaling["override_sessions"]
    elif "template" in phase:
        template = phase.get("template") or []
    else:
        template = _default_template(phase)
    sessions = []
    for spec in template:
        weekday = WEEKDAYS.get(str(spec.get("weekday", "")).upper())
        if weekday is None:
            continue
        day = monday + timedelta(days=weekday)
        if day < start or day > end:
            continue
        sessions.append(_materialize_session(spec, day, monday))
    return sessions


def _attach_library(sessions: list[dict[str, Any]], template: list[dict[str, Any]]) -> None:
    """Keep library names only for policy checks, not in the export."""
    return None


def session_policy(season: dict[str, Any], today: date) -> dict[str, Any]:
    phase = current_phase(season, today)
    now_names = list((phase or {}).get("key_sessions") or [])
    later = []
    for item in _phases(season):
        if phase is not None and item.get("start", "") <= phase.get("start", ""):
            continue
        later.append(
            {
                "phase": item.get("name"),
                "start": item.get("start"),
                "key_sessions": list(item.get("key_sessions") or []),
            }
        )
    return {"now": now_names, "later": later}


def later_session_names(policy: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    now = set(policy.get("now") or [])
    for block in policy.get("later") or []:
        for name in block.get("key_sessions") or []:
            if name not in now:
                names.add(name)
    return names


def week_km_estimate(sessions: list[dict[str, Any]]) -> float | None:
    if not sessions:
        return None
    total = sum(float(session.get("distance_km_estimate") or 0) for session in sessions)
    return round(total, 1)


def scaling_block(band: dict[str, Any], sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "phase_weekly_km": band.get("phase_weekly_km"),
        "this_week_target_km": band.get("this_week_target_km"),
        "overridden": bool(band.get("overridden")),
        "override_reason": band.get("override_reason"),
        "hard_pct": band.get("hard_pct"),
        "deload_week": bool(band.get("deload_week")),
        "week_km_estimate": week_km_estimate(sessions),
    }


def attempt_windows(season: dict[str, Any]) -> list[dict[str, Any]]:
    """Windows where a goal date must not fall. Not a ban on training sessions.

    ``blocked_windows`` is the old name and means the same thing.
    """
    raw = season.get("constraints") or {}
    windows = raw.get("no_attempt_windows")
    if windows is None:
        windows = raw.get("blocked_windows") or []
    return list(windows)


def constraints_block(season: dict[str, Any]) -> dict[str, Any]:
    raw = season.get("constraints") or {}
    return {
        "unavailable_weekdays": list(raw.get("unavailable_weekdays") or []),
        "no_attempt_windows": attempt_windows(season),
        "long_run_days": list(raw.get("long_run_days") or []),
        "preferred_training_time": raw.get("preferred_training_time"),
        "timezone": season.get("timezone"),
    }


def where_we_are(season: dict[str, Any], today: date) -> dict[str, Any]:
    phase = current_phase(season, today)
    active = goal_by_id(season, season.get("active_goal_id"))
    nxt = next_goal_after(season, active)
    later_goals = []
    goals = _goals(season)
    if nxt is not None:
        nxt_date = nxt.get("date") or ""
        for goal in goals:
            if (goal.get("date") or "") > nxt_date:
                later_goals.append(_public_goal(goal, today=today))
    days_to_goal = None
    active_passed = False
    if active and active.get("date"):
        active_date = _parse_date(active["date"])
        if active_date >= today:
            days_to_goal = (active_date - today).days
        elif nxt and nxt.get("date"):
            active_passed = True
            days_to_goal = (_parse_date(nxt["date"]) - today).days
        else:
            active_passed = True
            days_to_goal = (active_date - today).days
    days_to_next = None
    if nxt and nxt.get("date"):
        days_to_next = (_parse_date(nxt["date"]) - today).days
    block: dict[str, Any] = {
        "phase": phase.get("name") if phase else None,
        "phase_start": phase.get("start") if phase else None,
        "phase_end": phase_bounds(season, phase)[1].isoformat() if phase else None,
        "week_start": week_monday(today).isoformat(),
        "week_in_phase": week_in_phase(phase, today) if phase else None,
        "phase_weeks_total": phase_weeks_total(season, phase) if phase else None,
        "current_goal": _public_goal(active),
        "days_to_goal": days_to_goal,
        "next_goal": _public_goal(nxt),
        "days_to_next_goal": days_to_next,
        "later_goals": later_goals,
    }
    block["active_goal_date_passed"] = active_passed
    return block


def _normalize_title(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return "".join(ch for ch in folded.lower() if ch.isalnum())


def garmin_date_mismatch(
    season: dict[str, Any], events: list[dict[str, Any]]
) -> str | None:
    active = goal_by_id(season, season.get("active_goal_id"))
    if not active or not active.get("date"):
        return None
    expected = active.get("garmin_event_title") or active.get("title") or ""
    expected_norm = _normalize_title(str(expected))
    season_date = active["date"]
    for event in events:
        title = str(event.get("title") or "")
        if _normalize_title(title) != expected_norm and expected_norm not in _normalize_title(title):
            continue
        event_date = str(event.get("date") or "")[:10]
        if event_date and event_date != season_date:
            return (
                f"Garmin lists {title} on {event_date}. "
                f"The season date is {season_date}."
            )
    return None


def adherence_counts(
    season: dict[str, Any],
    snapshot: dict[str, Any] | None,
    today: date,
) -> dict[str, int]:
    counts = {
        "weeks_on_target": 0,
        "weeks_missed": 0,
        "weeks_over": 0,
        "weeks_unknown": 0,
        "evaluated_weeks": 0,
    }
    covered = set((snapshot or {}).get("covered_weeks") or [])
    km_by_week = (snapshot or {}).get("run_km_by_week") or {}
    seen: set[str] = set()
    for phase in _phases(season):
        start, end = phase_bounds(season, phase)
        monday = week_monday(start)
        while monday <= end:
            sunday = monday + timedelta(days=6)
            open_week = monday <= today <= sunday
            if (sunday < today or open_week) and monday.isoformat() not in seen:
                seen.add(monday.isoformat())
                if monday < start and sunday < start:
                    monday += timedelta(days=7)
                    continue
                band = band_for_week(season, monday)
                target = band.get("this_week_target_km")
                key = monday.isoformat()
                if open_week or key not in covered or target is None:
                    counts["weeks_unknown"] += 1
                else:
                    actual = float(km_by_week.get(key, 0))
                    low, high = float(target[0]), float(target[1])
                    if actual < low:
                        counts["weeks_missed"] += 1
                    elif actual > high:
                        counts["weeks_over"] += 1
                    else:
                        counts["weeks_on_target"] += 1
                    counts["evaluated_weeks"] += 1
            monday += timedelta(days=7)
    return counts


def promote_last_race(season: dict[str, Any], today: date) -> bool:
    """When an A or B goal date has passed, it becomes ``last_race`` if it is newer.

    A stored race that is already newer is left alone. Other priorities are not
    promoted. Returns whether the document changed.
    """
    best = season.get("last_race") if isinstance(season.get("last_race"), dict) else None
    best_date = str((best or {}).get("date") or "")
    for goal in _goals(season):
        priority = str(goal.get("priority") or "")
        if priority not in {"A", "B"}:
            continue
        raw = str(goal.get("date") or "")
        if not raw or _parse_date(raw) >= today:
            continue
        if raw > best_date:
            best = {
                "title": goal.get("title") or goal.get("goal_id"),
                "date": raw,
                "goal_id": goal.get("goal_id"),
            }
            best_date = raw
    current = season.get("last_race") if isinstance(season.get("last_race"), dict) else {}
    if not best or best.get("date") == current.get("date") and best.get("title") == current.get("title"):
        return False
    season["last_race"] = best
    return True


def days_since_race(season: dict[str, Any] | None, today: date) -> int | None:
    """Days since ``last_race.date`` on the season. Not read from Garmin activities."""
    last = (season or {}).get("last_race") or {}
    raw = last.get("date")
    if not raw:
        return None
    raced = _parse_date(str(raw))
    if raced > today:
        return None
    return (today - raced).days


def attempt_window_warnings(season: dict[str, Any]) -> list[str]:
    """Warn when a goal date sits in a no-attempt window. Sessions do not."""
    warnings = []
    for goal in _goals(season):
        goal_date = str(goal.get("date") or "")
        if not goal_date:
            continue
        for window in attempt_windows(season):
            start = str(window.get("from") or "")
            end = str(window.get("to") or "")
            if start and end and start <= goal_date <= end:
                reason = window.get("reason") or "no attempts"
                title = goal.get("title") or goal.get("goal_id")
                warnings.append(
                    f"{title} on {goal_date} falls in a no-attempt window ({reason})."
                )
    return warnings


def longest_run_warning(goal: dict[str, Any] | None, longest_km: float | None) -> str | None:
    if not goal or "min_longest_run_km" not in goal:
        return None
    floor = goal.get("min_longest_run_km")
    if floor is None:
        return None
    if longest_km is None or float(longest_km) < float(floor):
        return (
            f"Longest run {longest_km} km is below the goal floor of {floor} km."
        )
    return None


def volume_warning(
    estimate: float | None,
    snapshot: dict[str, Any] | None,
    *,
    refresh_failed: bool,
) -> str | None:
    """+15% only. A cut, a missing snapshot, a bad zero, or a failed refresh do not warn."""
    if refresh_failed or not snapshot or snapshot.get("as_of") is None:
        return None
    if estimate is None:
        return None
    avg = snapshot.get("last_4_week_avg_run_km")
    if avg is None:
        return None
    runs_present = bool(snapshot.get("runs_present"))
    if not runs_present:
        return None
    if runs_present and float(avg) == 0:
        return None
    if float(estimate) < float(avg):
        return None
    if float(estimate) > float(avg) * 1.15:
        return "Weekly volume is more than 15% above the 4-week average."
    return None


def _recovery_or_taper(phase: dict[str, Any] | None) -> bool:
    if not phase:
        return False
    kind = str(phase.get("kind") or phase.get("name") or "").lower()
    return kind in {"recovery", "taper", "taper_first", "taper_final"} or kind.startswith("taper")


def _build_or_peak(phase: dict[str, Any] | None) -> bool:
    if not phase:
        return False
    kind = str(phase.get("kind") or phase.get("name") or "").lower()
    return kind in {"build", "peak"}


def schedule_warnings(
    season: dict[str, Any],
    sessions: list[dict[str, Any]],
    *,
    today: date,
    snapshot: dict[str, Any] | None,
    refresh_failed: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return warnings and the sessions that are allowed to upload.

    The only hard block is a session on an unavailable weekday.
    """
    warnings: list[str] = []
    allowed: list[dict[str, Any]] = []
    constraints = season.get("constraints") or {}
    unavailable = {day.upper() for day in constraints.get("unavailable_weekdays") or []}
    policy = session_policy(season, today)
    later_names = later_session_names(policy)
    phase = current_phase(season, today)
    band = band_for_week(season, week_monday(today))
    hard_pct = band.get("hard_pct")
    active = goal_by_id(season, season.get("active_goal_id"))

    for session in sessions:
        day = _parse_date(session["date"])
        weekday = list(WEEKDAYS.keys())[day.weekday()]
        if weekday in unavailable:
            warnings.append(
                f"Blocked {session['name']} on {session['date']}: unavailable weekday."
            )
            continue
        allowed.append(session)
        identifiers = {
            session.get("library"),
            session.get("name"),
            name_slug(str(session.get("name") or "")).replace("-", "_"),
        }
        matched = next((name for name in identifiers if name and name in later_names), None)
        if matched:
            warnings.append(
                f"{session['name']} is only in a later phase ({matched})."
            )
        if session.get("intent") == "hard" and active and active.get("date"):
            goal_day = _parse_date(active["date"])
            priority = str(active.get("priority") or "")
            if priority in {"A", "B"} and abs((goal_day - day).days) <= 7:
                warnings.append(
                    f"Hard session {session['name']} is within 7 days of an A or B goal."
                )

    if _build_or_peak(phase) and not any(session.get("intent") == "long" for session in allowed):
        warnings.append("Build or peak week has no session with intent long.")
    if _recovery_or_taper(phase) and any(session.get("intent") == "hard" for session in allowed):
        warnings.append("Hard work in a recovery or taper phase.")
    if hard_pct is not None and allowed:
        hard_minutes = sum(
            float(session.get("minutes") or 0)
            for session in allowed
            if session.get("intent") == "hard"
        )
        total_minutes = sum(float(session.get("minutes") or 0) for session in allowed)
        if total_minutes > 0 and (100.0 * hard_minutes / total_minutes) > float(hard_pct):
            warnings.append(f"Phase hard_pct {hard_pct} is exceeded.")
    estimate = week_km_estimate(allowed)
    volume = volume_warning(estimate, snapshot, refresh_failed=refresh_failed)
    if volume:
        warnings.append(volume)
    return warnings, allowed


def export_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    exported = []
    for session in sessions:
        exported.append(
            {
                "date": session["date"],
                "type": session["type"],
                "intent": session["intent"],
                "name": session["name"],
                "minutes": session["minutes"],
                "zone": session["zone"],
                "distance_km_estimate": session["distance_km_estimate"],
                "note": session.get("note"),
                "library": session.get("library"),
                "idempotency_key": session["idempotency_key"],
                "steps": session["steps"],
            }
        )
    return exported


def validate_season(document: dict[str, Any]) -> None:
    if not document.get("timezone"):
        raise ValueError("timezone is required.")
    if not document.get("season_id"):
        raise ValueError("season_id is required.")
    for phase in document.get("phases") or []:
        if phase.get("deload_every_n_weeks") and phase.get("deload_factor") is None:
            raise ValueError(
                f"Phase {phase.get('name')} sets deload_every_n_weeks without deload_factor."
            )


def example_season() -> dict[str, Any]:
    """Working-model season behind the 26 October 2026 overview."""
    return {
        "season_id": "2027",
        "timezone": "Europe/Brussels",
        "active_goal_id": "pacte_siberien",
        "goals": [
            {
                "goal_id": "pacte_siberien",
                "title": "Le Pacte Sibérien — Pact 12",
                "date": "2027-04-24",
                "type": "backyard_ultra",
                "priority": "B",
                "garmin_event_title": "Pacte Sibérien",
                "min_longest_run_km": None,
            },
            {
                "goal_id": "lee_trail_fkt",
                "title": "Lee Trail FKT",
                "date": "2027-06-26",
                "priority": "A",
            },
            {
                "goal_id": "uths_100",
                "title": "UTHS 100",
                "date": "2027-08-01",
                "priority": "A",
            },
        ],
        "constraints": {
            "no_attempt_windows": [
                {
                    "from": "2026-10-15",
                    "to": "2027-02-15",
                    "reason": "hunting season — no FKT attempts",
                }
            ],
            "preferred_training_time": "evening",
            "unavailable_weekdays": [],
            "long_run_days": ["SATURDAY", "SUNDAY"],
        },
        "last_race": {"title": "Antwerp Marathon", "date": "2026-10-18"},
        "phases": [
            {
                "name": "return_to_run",
                "start": "2026-10-26",
                "end": "2026-11-08",
                "weekly_km": [10, 25],
                "hard_pct": 0,
                "key_sessions": [],
                "template": [
                    {
                        "weekday": "WEDNESDAY",
                        "library": "easy",
                        "minutes": 30,
                        "zone": 1,
                        "name": "Wed Easy Z1 (foot test)",
                        "note": "foot test",
                        "intent": "easy",
                        "type": "easy",
                    },
                    {
                        "weekday": "FRIDAY",
                        "library": "easy",
                        "minutes": 30,
                        "zone": 1,
                        "name": "Fri Easy Z1",
                        "intent": "easy",
                        "type": "easy",
                    },
                    {
                        "weekday": "SUNDAY",
                        "library": "easy",
                        "minutes": 40,
                        "zone": 2,
                        "name": "Sun Easy",
                        "intent": "easy",
                        "type": "long",
                    },
                ],
            },
            {
                "name": "base",
                "start": "2026-11-09",
                "end": "2027-01-10",
                "weekly_km": [25, 55],
                "hard_pct": 10,
                "key_sessions": ["long_run", "vert_repeats"],
            },
            {
                "name": "build",
                "start": "2027-01-11",
                "end": "2027-03-14",
                "weekly_km": [55, 80],
                "hard_pct": 15,
                "deload_every_n_weeks": 4,
                "deload_factor": 0.7,
                "key_sessions": ["loop_rehearsal", "back_to_back", "transition_drill"],
            },
            {
                "name": "peak",
                "start": "2027-03-15",
                "end": "2027-04-24",
                "weekly_km": [40, 70],
                "hard_pct": 10,
                "key_sessions": ["dress_rehearsal_8_loops"],
            },
        ],
        "week_overrides": [],
    }


def what_you_can_do(
    *,
    has_sessions: bool,
    mismatch: str | None,
    goal_date: str | None,
    race_on_calendar: bool,
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if has_sessions:
        actions.append(
            {"when": "Push the sessions above", "tool": "schedule_week", "pass": "sessions"}
        )
    actions.append(
        {
            "when": "The session is a loop, a pace repeat, or anything a template cannot express",
            "tool": "create_workout",
        }
    )
    if mismatch and goal_date:
        actions.append(
            {
                "when": f"Move the Garmin race date onto {goal_date}",
                "tool": "add_event, then remove_event, then get_events",
            }
        )
    elif goal_date and not race_on_calendar:
        actions.append(
            {
                "when": "The agreed race date is not on the calendar",
                "tool": "add_event, then get_events",
            }
        )
    actions.append({"when": "Record why a week left the template", "tool": "add_plan_note"})
    actions.append(
        {"when": "A junk calendar row should go", "tool": "delete_scheduled_workout"}
    )
    actions.append({"when": "A library workout should go", "tool": "delete_workout"})
    return actions


def load_context_from_brief(brief: dict[str, Any] | None, *, hard_pct: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"label": LOAD_LABEL}
    if not isinstance(brief, dict):
        context["error"] = "coaching brief unavailable"
        return context
    if brief.get("error") and "coaching_brief" not in brief and "next_week_proposal" not in brief:
        context["error"] = brief.get("error")
        return context
    proposal = brief.get("next_week_proposal")
    inner = brief.get("coaching_brief")
    if proposal is None and isinstance(inner, dict):
        proposal = inner.get("next_week_proposal")
    if not isinstance(proposal, dict):
        return context
    if hard_pct == 0:
        kept: dict[str, Any] = {}
        for key in ("week_type", "target_min", "chronic_min", "outlier_weeks_dropped"):
            value = proposal.get(key)
            if value is None:
                continue
            if key == "outlier_weeks_dropped" and value == []:
                continue
            kept[key] = value
        context["next_week_proposal"] = kept
        return context
    context["next_week_proposal"] = proposal
    return context


def status_warnings(
    season: dict[str, Any],
    today: date,
    snapshot: dict[str, Any] | None,
    *,
    refresh_failed: bool,
    events: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
) -> list[str]:
    warnings: list[str] = []
    phase = current_phase(season, today)
    band = band_for_week(season, today)
    if band.get("missing_week_band"):
        warnings.append("missing_week_band")
    where = where_we_are(season, today)
    if where.get("active_goal_date_passed"):
        warnings.append("active_goal_date_passed")
    event_rows = events
    if event_rows is None and snapshot:
        event_rows = snapshot.get("events") or []
    mismatch = garmin_date_mismatch(season, event_rows or [])
    if mismatch:
        warnings.append(mismatch)
    active = goal_by_id(season, season.get("active_goal_id"))
    longest = None if not snapshot else snapshot.get("longest_run_last_28_days_km")
    floor = longest_run_warning(active, longest)
    if floor:
        warnings.append(floor)
    estimate = week_km_estimate(sessions or [])
    volume = volume_warning(estimate, snapshot, refresh_failed=refresh_failed)
    if volume:
        warnings.append(volume)
    if phase is None and _phases(season):
        warnings.append("no_current_phase")
    warnings.extend(attempt_window_warnings(season))
    return warnings
