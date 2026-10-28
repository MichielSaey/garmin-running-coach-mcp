"""Calendar reads and event writes. garminconnect 0.3.6 does not wrap events."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Optional

CallOptional = Callable[..., Any]

_SKIP_ITEM_TYPES = {"event", "activity", "nap", "sleep", "note"}
_EVENT_CREATE_PATH = "/calendar-service/event"


def item_date(item: dict[str, Any]) -> Optional[str]:
    for key in ("date", "calendarDate"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value[:10]
    return None


def workout_id_from_item(item: dict[str, Any]) -> Optional[int]:
    for key in ("workoutId", "workoutTemplateId"):
        value = item.get(key)
        if value is not None:
            return int(value)
    workout = item.get("workout")
    if isinstance(workout, dict) and workout.get("workoutId") is not None:
        return int(workout["workoutId"])
    return None


def schedule_id_from_item(item: dict[str, Any]) -> Optional[int]:
    for key in ("workoutScheduleId", "scheduledWorkoutId", "scheduleId"):
        value = item.get(key)
        if value is not None:
            return int(value)
    if item.get("itemType") == "workout" and item.get("id") is not None:
        return int(item["id"])
    return None


def event_id_from_item(item: dict[str, Any]) -> Optional[int]:
    for key in ("id", "eventId", "calendarItemId"):
        value = item.get(key)
        if value is not None:
            return int(value)
    return None


def item_name(item: dict[str, Any]) -> Optional[str]:
    if item.get("title"):
        return str(item["title"])
    workout = item.get("workout")
    if isinstance(workout, dict) and workout.get("workoutName"):
        return str(workout["workoutName"])
    for key in ("workoutName", "name"):
        if item.get(key):
            return str(item[key])
    return None


def is_workout_item(item: dict[str, Any]) -> bool:
    kind = item.get("itemType")
    if kind in _SKIP_ITEM_TYPES:
        return False
    if kind == "workout":
        return True
    return workout_id_from_item(item) is not None


def tied_to_completed_activity(item: dict[str, Any]) -> bool:
    if item.get("itemType") == "activity":
        return True
    return bool(item.get("activityId") or item.get("completedActivityId"))


def _month_starts(start: date, end: date) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def calendar_items(
    client: Any,
    call_optional: CallOptional,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for year, month in _month_starts(start, end):
        payload = call_optional(client, "get_scheduled_workouts", year=year, month=month)
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        rows = payload.get("calendarItems", [])
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            day = item_date(item)
            if day is None or day < start.isoformat() or day > end.isoformat():
                continue
            items.append(item)
    return items


def scheduled_workouts_between(
    client: Any,
    call_optional: CallOptional,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    rows = []
    seen: set[tuple[str, int, int | None]] = set()
    for item in calendar_items(client, call_optional, start, end):
        if not is_workout_item(item):
            continue
        workout_id = workout_id_from_item(item)
        if workout_id is None:
            continue
        day = item_date(item) or ""
        schedule_id = schedule_id_from_item(item)
        key = (day, workout_id, schedule_id)
        if key in seen:
            continue
        seen.add(key)
        row = {
            "date": day,
            "name": item_name(item),
            "workout_id": workout_id,
        }
        if schedule_id is not None:
            row["schedule_id"] = schedule_id
        rows.append(row)
    rows.sort(key=lambda row: (row["date"] or "", row["workout_id"]))
    return rows


def events_between(
    client: Any,
    call_optional: CallOptional,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    events = []
    seen: set[tuple[Any, str]] = set()
    for item in calendar_items(client, call_optional, start, end):
        if item.get("itemType") != "event":
            continue
        day = item_date(item)
        title = item_name(item) or ""
        event_id = event_id_from_item(item)
        key = (event_id, day or "")
        if key in seen:
            continue
        seen.add(key)
        events.append({"event_id": event_id, "title": title, "date": day})
    events.sort(key=lambda row: row["date"] or "")
    return events


def _http(client: Any) -> Any:
    http = getattr(client, "client", None)
    if http is None:
        raise RuntimeError("Garmin client has no calendar HTTP client.")
    return http


def _clone_event_payload(
    template: dict[str, Any],
    *,
    title: str,
    event_date: str,
    distance_meters: float | None,
    duration_minutes: float | None,
) -> dict[str, Any]:
    skip = {"id", "eventId", "calendarItemId", "itemType", "ownerId", "userId"}
    payload = {key: value for key, value in template.items() if key not in skip}
    payload["title"] = title
    payload["date"] = event_date
    if "calendarDate" in payload:
        payload["calendarDate"] = event_date
    if distance_meters is not None:
        target = dict(template.get("completionTarget") or {})
        target["value"] = distance_meters
        target.setdefault("unit", "meter")
        target["unitType"] = "distance"
        payload["completionTarget"] = target
    elif duration_minutes is not None:
        target = dict(template.get("completionTarget") or {})
        target["value"] = duration_minutes
        target.setdefault("unit", "minute")
        target["unitType"] = "duration"
        payload["completionTarget"] = target
    return payload


def add_event(
    client: Any,
    call_optional: CallOptional,
    *,
    title: str,
    event_date: str,
    distance_meters: float | None = None,
    duration_minutes: float | None = None,
) -> dict[str, Any]:
    """Create a calendar event and only report it when a following read shows it."""
    try:
        day = date.fromisoformat(event_date)
    except ValueError:
        return {"error": "date must be an ISO date (YYYY-MM-DD)"}

    window_start = day.replace(day=1)
    if day.month == 12:
        month_end = date(day.year, 12, 31)
    else:
        month_end = date(day.year, day.month + 1, 1) - timedelta(days=1)
    existing = events_between(client, call_optional, window_start, month_end)
    for event in existing:
        if event.get("title") == title and event.get("date") == event_date:
            return {"ok": True, "created": False, "event": event}

    template_item = None
    for item in calendar_items(client, call_optional, window_start, month_end):
        if item.get("itemType") == "event":
            template_item = item
            break
    if template_item is None:
        # A payload has to be copied from a real event. Search the surrounding year.
        year_start = date(day.year, 1, 1)
        year_end = date(day.year, 12, 31)
        for item in calendar_items(client, call_optional, year_start, year_end):
            if item.get("itemType") == "event":
                template_item = item
                break
    if template_item is None:
        return {
            "error": "add_event needs an existing calendar event to copy the create payload.",
        }

    payload = _clone_event_payload(
        template_item,
        title=title,
        event_date=event_date,
        distance_meters=distance_meters,
        duration_minutes=duration_minutes,
    )
    try:
        http = _http(client)
        http.post("connectapi", _EVENT_CREATE_PATH, json=payload, api=True)
    except Exception as exc:
        return {"error": f"add_event failed: {exc}"}

    confirmed = events_between(client, call_optional, window_start, month_end)
    for event in confirmed:
        if event.get("title") == title and event.get("date") == event_date:
            return {"ok": True, "created": True, "event": event}
    return {
        "error": "add_event did not stick: get_events does not show that title and date.",
    }


def remove_event(client: Any, event_id: int) -> dict[str, Any]:
    try:
        http = _http(client)
        http.delete("connectapi", f"{_EVENT_CREATE_PATH}/{int(event_id)}", api=True)
    except Exception as exc:
        return {"error": f"remove_event failed: {exc}"}
    return {"ok": True, "event_id": int(event_id)}
