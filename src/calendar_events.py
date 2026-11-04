"""Calendar reads and event writes. garminconnect 0.3.6 does not wrap events."""

from __future__ import annotations

import copy
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


def item_sport(item: dict[str, Any]) -> Optional[str]:
    sport = item.get("sport")
    if isinstance(sport, str) and sport.strip():
        return sport
    sport_type = item.get("sportType")
    if isinstance(sport_type, dict):
        key = sport_type.get("sportTypeKey") or sport_type.get("key")
        if key:
            return str(key)
    if isinstance(sport_type, str) and sport_type.strip():
        return sport_type
    return None


def item_source(item: dict[str, Any]) -> Optional[str]:
    for key in ("source", "workoutSource", "scheduleSource"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def item_name(item: dict[str, Any]) -> Optional[str]:
    if item.get("title"):
        return str(item["title"])
    workout = item.get("workout")
    if isinstance(workout, dict) and workout.get("workoutName"):
        return str(workout["workoutName"])
    for key in ("eventName", "workoutName", "name"):
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
        sport = item_sport(item)
        if sport is not None:
            row["sport"] = sport
        source = item_source(item)
        if source is not None:
            row["source"] = source
        rows.append(row)
    rows.sort(key=lambda row: (row["date"] or "", row["workout_id"]))
    return rows


def calendar_rows_for_workout(
    client: Any,
    call_optional: CallOptional,
    workout_id: int,
    *,
    around: date,
) -> list[dict[str, Any]]:
    """Calendar rows that still point at a library workout. Does not clear them."""
    start = around - timedelta(days=21)
    end = around + timedelta(days=180)
    wanted = int(workout_id)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    for item in calendar_items(client, call_optional, start, end):
        if not is_workout_item(item) or workout_id_from_item(item) != wanted:
            continue
        day = item_date(item) or ""
        schedule_id = schedule_id_from_item(item)
        key = (schedule_id, day)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "schedule_id": schedule_id,
                "date": day or None,
                "name": item_name(item),
            }
        )
    rows.sort(key=lambda row: (row["date"] or "", row["schedule_id"] or 0))
    return rows


def find_scheduled_by_id(
    client: Any,
    call_optional: CallOptional,
    schedule_id: int,
    *,
    around: date,
) -> dict[str, Any] | None:
    """The calendar workout whose schedule id is ``schedule_id``."""
    start = around - timedelta(days=120)
    end = around + timedelta(days=220)
    wanted = int(schedule_id)
    for item in calendar_items(client, call_optional, start, end):
        if not is_workout_item(item):
            continue
        if schedule_id_from_item(item) == wanted:
            return item
    return None


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
        events.append(event_public_row(item))
    events.sort(key=lambda row: row["date"] or "")
    return events


def completion_target(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("completionTarget")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "value": raw.get("value"),
        "unit": raw.get("unit"),
        "unitType": raw.get("unitType"),
    }


def event_public_row(item: dict[str, Any]) -> dict[str, Any]:
    """Event fields a coach can check: identity plus the completion target."""
    return {
        "event_id": event_id_from_item(item),
        "title": item_name(item) or "",
        "date": item_date(item),
        "target": completion_target(item),
    }


def _http(client: Any) -> Any:
    http = getattr(client, "client", None)
    if http is None:
        raise RuntimeError("Garmin client has no calendar HTTP client.")
    return http


# Calendar-feed items carry activity-type metadata. The event endpoint cannot
# read it, so echoing it back 500s ("EventActivityType:90").
_MONTH_FEED_ITEM_KEYS = {"activityTypeId", "activityType"}


def _sport_key(value: Any) -> str | None:
    """The string sport key Garmin's event endpoint accepts."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("typeKey", "key", "sportTypeKey"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _time_target(minutes: float) -> dict[str, Any]:
    """A time-based completion target. Garmin stores whole seconds as unitType time."""
    return {
        "value": int(round(float(minutes) * 60.0)),
        "unit": "second",
        "unitType": "time",
    }


def _clone_event_payload(
    template: dict[str, Any],
    *,
    title: str,
    event_date: str,
    distance_meters: float | None,
    duration_minutes: float | None,
) -> dict[str, Any]:
    skip = (
        {"id", "eventId", "calendarItemId", "itemType", "ownerId", "userId"}
        | _MONTH_FEED_ITEM_KEYS
    )
    payload = {
        key: copy.deepcopy(value)
        for key, value in template.items()
        if key not in skip
    }
    # Calendar list items expose `title`. The create API requires `eventName`.
    payload["title"] = title
    payload["eventName"] = title
    payload["name"] = title
    payload["date"] = event_date
    # eventType has to be a sport key string; a feed object or id 500s.
    sport_key = _sport_key(payload.get("eventType"))
    if sport_key is not None:
        payload["eventType"] = sport_key
    else:
        payload.pop("eventType", None)
    # Garmin validates eventName on the body and on nested arg objects.
    _stamp_event_title(payload, title)
    if "calendarDate" in payload:
        payload["calendarDate"] = event_date
    if distance_meters is not None:
        payload["completionTarget"] = {
            "value": distance_meters,
            "unit": "meter",
            "unitType": "distance",
        }
    elif duration_minutes is not None:
        payload["completionTarget"] = _time_target(duration_minutes)
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


def _blank_event_name(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _stamp_event_title(node: Any, title: str) -> None:
    """Write ``title`` into every eventName, and into a blank name or title."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "eventName":
                node[key] = title
            elif key in {"name", "title"} and _blank_event_name(value):
                node[key] = title
            else:
                _stamp_event_title(value, title)
    elif isinstance(node, list):
        for item in node:
            _stamp_event_title(item, title)


def _find_event_item(
    client: Any,
    call_optional: CallOptional,
    event_id: int,
) -> dict[str, Any] | None:
    today = date.today()
    start = date(today.year - 1, 1, 1)
    end = date(today.year + 1, 12, 31)
    wanted = int(event_id)
    for year, month in _month_starts(start, end):
        payload = call_optional(client, "get_scheduled_workouts", year=year, month=month)
        if not isinstance(payload, dict) or payload.get("error"):
            continue
        rows = payload.get("calendarItems", [])
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict) or item.get("itemType") != "event":
                continue
            if event_id_from_item(item) == wanted:
                return item
    return None


def _duration_minutes_of(target: dict[str, Any]) -> float | None:
    if target.get("unitType") not in {"duration", "time"} or target.get("value") is None:
        return None
    value = float(target["value"])
    unit = str(target.get("unit") or "")
    if unit == "hour":
        return value * 60.0
    if unit == "second":
        return value / 60.0
    if unit == "minute":
        return value
    return None


def _distance_meters_of(target: dict[str, Any]) -> float | None:
    if target.get("unitType") != "distance" or target.get("value") is None:
        return None
    value = float(target["value"])
    unit = str(target.get("unit") or "")
    if unit in {"meter", "m"}:
        return value
    if unit in {"km", "kilometer"}:
        return value * 1000.0
    if unit == "mile":
        return value * 1609.344
    return None


def _change_is_visible(
    row: dict[str, Any],
    *,
    title: str | None,
    event_date: str | None,
    distance_meters: float | None,
    duration_minutes: float | None,
) -> bool:
    if title is not None and row.get("title") != title:
        return False
    if event_date is not None and row.get("date") != event_date:
        return False
    target = row.get("target") if isinstance(row.get("target"), dict) else {}
    if distance_meters is not None:
        meters = _distance_meters_of(target)
        if meters is None or abs(meters - float(distance_meters)) > 1:
            return False
    if duration_minutes is not None:
        minutes = _duration_minutes_of(target)
        if minutes is None or abs(minutes - float(duration_minutes)) > 0.1:
            return False
    return True


def get_event(
    client: Any,
    call_optional: CallOptional,
    event_id: int,
) -> dict[str, Any]:
    """One calendar event, including its completion target."""
    item = _find_event_item(client, call_optional, event_id)
    if item is None:
        return {"error": f"No calendar event {int(event_id)}."}
    return {"ok": True, "event": event_public_row(item)}


def _read_event_resource(client: Any, event_id: int) -> dict[str, Any]:
    """The stored event as ``/calendar-service/event/{id}`` returns it."""
    payload = _http(client).connectapi(f"{_EVENT_CREATE_PATH}/{int(event_id)}")
    if not isinstance(payload, dict):
        return {}
    return payload


def update_event(
    client: Any,
    call_optional: CallOptional,
    *,
    event_id: int,
    title: str | None = None,
    event_date: str | None = None,
    distance_meters: float | None = None,
    duration_minutes: float | None = None,
) -> dict[str, Any]:
    """Read the stored event, overlay the change, PUT the whole record, read back."""
    if distance_meters is not None and duration_minutes is not None:
        return {"error": "Pass distance_meters or duration_minutes, not both."}
    if title is not None and not str(title).strip():
        return {"error": "title must not be blank"}
    if event_date is not None:
        try:
            date.fromisoformat(event_date)
        except ValueError:
            return {"error": "date must be an ISO date (YYYY-MM-DD)"}

    try:
        record = _read_event_resource(client, event_id)
    except Exception as exc:
        return {"error": f"update_event failed: {exc}"}
    if not record:
        return {"error": f"No calendar event {int(event_id)}."}

    payload = copy.deepcopy(record)
    if title is not None:
        payload["eventName"] = title
    if event_date is not None:
        payload["date"] = event_date
    if distance_meters is not None:
        payload["completionTarget"] = {
            "value": distance_meters,
            "unit": "meter",
            "unitType": "distance",
        }
    elif duration_minutes is not None:
        payload["completionTarget"] = _time_target(duration_minutes)

    try:
        http = _http(client)
        http.put(
            "connectapi",
            f"{_EVENT_CREATE_PATH}/{int(event_id)}",
            json=payload,
            api=True,
        )
    except Exception as exc:
        return {"error": f"update_event failed: {exc}"}

    try:
        confirmed = _read_event_resource(client, event_id)
    except Exception as exc:
        return {"error": f"update_event failed: {exc}"}
    if not confirmed:
        return {"error": "update_event did not stick: get_event does not show that id."}
    row = event_public_row(confirmed)
    if not _change_is_visible(
        row,
        title=title,
        event_date=event_date,
        distance_meters=distance_meters,
        duration_minutes=duration_minutes,
    ):
        return {
            "error": "update_event did not stick: the following read does not show the change.",
        }
    return {"ok": True, "event": row}


def remove_event(client: Any, event_id: int) -> dict[str, Any]:
    try:
        http = _http(client)
        http.delete("connectapi", f"{_EVENT_CREATE_PATH}/{int(event_id)}", api=True)
    except Exception as exc:
        return {"error": f"remove_event failed: {exc}"}
    return {"ok": True, "event_id": int(event_id)}
