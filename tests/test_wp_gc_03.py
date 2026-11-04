"""WP-GC-03: closed steps, season status, schedule idempotency, overview shape."""

from __future__ import annotations

import os
from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from garminconnect import GarminConnectAuthenticationError

import server
from coaching import build_coaching_overview, summarize_activities
from plan_store import get_schedule_record, save_actuals, save_schedule_record
from scheduling import schedule_week as run_schedule_week
from scheduling import sync_block as run_sync_block
from season_plan import (
    band_for_week,
    example_season,
    export_sessions,
    promote_last_race,
    resolve_sessions,
    schedule_warnings,
    status_warnings,
    volume_warning,
    what_you_can_do,
)
from step_schema import estimate_minutes, estimate_seconds, normalize_steps


@pytest.fixture
def plan_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_PLAN_DB", str(tmp_path / "plan.db"))
    yield tmp_path / "plan.db"


def test_loop_rehearsal_is_five_by_fifty_three_minutes() -> None:
    steps = normalize_steps(
        [
            {
                "type": "repeat",
                "iterations": 5,
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "time", "minutes": 45},
                        "target": {"type": "hr_zone", "zone": 2},
                    },
                    {
                        "type": "recovery",
                        "end": {"type": "time", "minutes": 8},
                        "target": {"type": "hr_zone", "zone": 1},
                    },
                ],
            }
        ]
    )
    assert steps[0]["iterations"] == 5
    assert len(steps[0]["steps"]) == 2
    assert estimate_minutes(steps) == 265.0
    assert estimate_seconds(steps) == 5 * 53 * 60


def test_pace_repeats_keep_the_pace_target() -> None:
    steps = normalize_steps(
        [
            {
                "type": "repeat",
                "iterations": 2,
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "time", "minutes": 24},
                        "target": {"type": "pace", "mps_low": 2.60, "mps_high": 2.78},
                    }
                ],
            }
        ]
    )
    target = steps[0]["steps"][0]["target"]
    assert target["type"] == "pace"
    assert target["mps_low"] == pytest.approx(2.60)
    assert target["mps_high"] == pytest.approx(2.78)
    assert estimate_minutes(steps) == 48.0
    assert steps[0]["iterations"] == 2
    # Not the sprint template default.
    assert "distance" not in steps[0]["steps"][0]["end"]["type"]


def test_distance_at_pace_estimates_between_126_and_135_minutes() -> None:
    steps = normalize_steps(
        [
            {
                "type": "interval",
                "end": {"type": "distance", "meters": 21100},
                "target": {"type": "pace", "mps_low": 2.60, "mps_high": 2.78},
            }
        ]
    )
    assert steps[0]["target"]["type"] == "pace"
    minutes = estimate_minutes(steps)
    assert 126 <= minutes <= 135


def test_backyard_rehearsal_uses_estimated_pace_not_a_template_default() -> None:
    pace = 2.5
    steps = normalize_steps(
        [
            {
                "type": "repeat",
                "iterations": 12,
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "distance", "meters": 6705.6},
                        "target": {"type": "hr_zone", "zone": 2},
                        "estimated_pace_mps": pace,
                    },
                    {
                        "type": "recovery",
                        "end": {"type": "time", "minutes": 5},
                        "target": {"type": "none"},
                    },
                ],
            }
        ]
    )
    expected = 12 * (6705.6 / pace + 300)
    assert steps[0]["iterations"] == 12
    assert steps[0]["steps"][0]["estimated_pace_mps"] == pace
    assert estimate_seconds(steps) == pytest.approx(expected, abs=2)


def test_unknown_step_key_and_segment_key_are_errors() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        normalize_steps(
            [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 10},
                    "target": {"type": "hr_zone", "zone": 1},
                    "nope": 1,
                }
            ]
        )
    result = server.combine_workout_templates(
        "Nope",
        [{"template": "sprint", "params": {"repetitions": 6}, "distance_meters": 3000}],
    )
    assert "error" in result
    assert "6" not in result["error"] or "Unknown" in result["error"]


def test_unsupported_sport_does_not_build_a_run() -> None:
    result = server.create_workout("Ride", [], sport="cycling")
    assert result["code"] == "unsupported_sport"


def test_sport_defaults_to_running_and_gravel_is_cycling() -> None:
    from workout_builder import build_running_workout

    running = build_running_workout(
        "Run",
        [{"type": "interval", "duration_minutes": 30, "heart_rate_zone": 2}],
    )
    gravel = build_running_workout(
        "Gravel",
        [{"type": "interval", "duration_minutes": 60, "heart_rate_zone": 2}],
        sport="gravel",
    )
    assert running.sportType["sportTypeKey"] == "running"
    assert running.workoutSegments[0].sportType["sportTypeKey"] == "running"
    assert gravel.sportType["sportTypeKey"] == "cycling"
    assert gravel.sportType["sportTypeId"] == 2
    assert gravel.workoutSegments[0].sportType["sportTypeKey"] == "cycling"
    assert type(gravel).__name__ == "CyclingWorkout"


def test_distance_hr_range_builds_and_echoes_the_bounds(plan_db) -> None:
    del plan_db
    steps = [
        {
            "type": "interval",
            "end": {"type": "distance", "meters": 17_000},
            "target": {"type": "hr_range", "min_bpm": 135, "max_bpm": 150},
        }
    ]
    canonical = normalize_steps(steps)
    assert canonical[0]["target"] == {"type": "hr_range", "min_bpm": 135, "max_bpm": 150}
    mock_client = MagicMock()
    mock_client.upload_cycling_workout.return_value = {"workoutId": 77}
    with (
        patch.object(server, "_get_client_or_error", return_value=(mock_client, None)),
        patch.object(server, "get_profile", return_value={}),
        patch.object(server, "resolve_hr_context", return_value={"zones": []}),
    ):
        result = server.create_workout("Gravel spin", steps, sport="gravel")
    assert result.get("error") is None
    assert result["resolved_steps"][0]["target"] == {
        "type": "hr_range",
        "min_bpm": 135,
        "max_bpm": 150,
    }
    workout = mock_client.upload_cycling_workout.call_args.args[0]
    built = workout.workoutSegments[0].workoutSteps[0]
    assert built.endConditionValue == 17000
    assert built.targetValueOne == 135
    assert built.targetValueTwo == 150
    assert result.get("estimatedDurationMinutes") != 1.0
    assert result.get("estimatedDurationMinutes") is None
    zoned = normalize_steps(
        [
            {
                "type": "interval",
                "end": {"type": "distance", "meters": 1000},
                "target": {"type": "hr_zone", "zone": 2},
            }
        ]
    )
    assert zoned[0]["target"]["type"] == "hr_zone"


def test_distance_hr_range_with_pace_reports_meters_over_pace(plan_db) -> None:
    del plan_db
    pace = 5.0
    steps = [
        {
            "type": "interval",
            "end": {"type": "distance", "meters": 17_000},
            "target": {"type": "hr_range", "min_bpm": 135, "max_bpm": 150},
            "estimated_pace_mps": pace,
        }
    ]
    mock_client = MagicMock()
    mock_client.upload_cycling_workout.return_value = {"workoutId": 78}
    with (
        patch.object(server, "_get_client_or_error", return_value=(mock_client, None)),
        patch.object(server, "get_profile", return_value={}),
        patch.object(server, "resolve_hr_context", return_value={"zones": []}),
    ):
        result = server.create_workout("Gravel spin", steps, sport="gravel")
    assert result.get("error") is None
    expected = 17_000 / pace / 60
    assert result["estimatedDurationMinutes"] == pytest.approx(expected, abs=0.2)
    assert result["estimatedDurationMinutes"] != 1.0


def test_short_timed_workout_still_builds(plan_db) -> None:
    del plan_db
    mock_client = MagicMock()
    mock_client.upload_running_workout.return_value = {"workoutId": 79}
    with (
        patch.object(server, "_get_client_or_error", return_value=(mock_client, None)),
        patch.object(server, "get_profile", return_value={}),
        patch.object(server, "resolve_hr_context", return_value={"zones": []}),
    ):
        result = server.create_workout(
            "Stride",
            [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 0.5},
                    "target": {"type": "hr_zone", "zone": 5},
                }
            ],
        )
    assert result.get("error") is None
    workout = mock_client.upload_running_workout.call_args.args[0]
    assert workout.estimatedDurationInSecs == 60
    assert result["estimatedDurationMinutes"] == 1.0


def test_create_workout_echoes_resolved_steps(plan_db) -> None:
    del plan_db
    mock_client = MagicMock()
    mock_client.upload_running_workout.return_value = {"workoutId": 42}
    with (
        patch.object(server, "_get_client_or_error", return_value=(mock_client, None)),
        patch.object(server, "get_profile", return_value={}),
        patch.object(server, "resolve_hr_context", return_value={"zones": []}),
    ):
        result = server.create_workout(
            "Pace",
            [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 24},
                    "target": {"type": "pace", "mps_low": 2.6, "mps_high": 2.78},
                }
            ],
        )
    assert result["workoutId"] == 42
    assert result["sport"] == "running"
    assert result["resolved_steps"][0]["target"]["type"] == "pace"
    assert result["estimatedDurationMinutes"] == 24.0


def test_two_week_step_and_long_phase_are_not_interpolated() -> None:
    season = example_season()
    opening = band_for_week(season, date(2026, 10, 28))
    assert opening["this_week_target_km"] == [10, 18]
    assert opening["overridden"] is False
    assert opening["override_reason"] is None
    second = band_for_week(season, date(2026, 11, 4))
    assert second["this_week_target_km"] == [10, 25]
    january = band_for_week(season, date(2027, 1, 13))
    assert january["this_week_target_km"] == [55, 80]
    assert january["deload_week"] is False
    deload = band_for_week(season, date(2027, 2, 3))
    assert deload["deload_week"] is True
    assert deload["this_week_target_km"] == [38.5, 56]
    assert deload["overridden"] is False


def test_week_override_wins_and_a_note_does_not(plan_db) -> None:
    season = example_season()
    season["week_overrides"] = [
        {"week_of": "2027-02-01", "weekly_km": [40, 45], "reason": "deload"}
    ]
    overridden = band_for_week(season, date(2027, 2, 3))
    assert overridden["overridden"] is True
    assert overridden["override_reason"] == "deload"
    assert overridden["this_week_target_km"] == [40, 45]
    assert overridden["deload_week"] is False

    server.set_season(example_season())
    noted = server.add_plan_note(
        date="2026-10-27",
        scope="week",
        key="2026-10-26",
        text="Foot note. Not a prescription.",
        tags=["injury"],
    )
    assert noted["ok"] is True
    status = server.get_plan_status()
    assert status["scaling"]["overridden"] is False
    assert status["scaling"]["override_reason"] is None
    assert status["scaling"]["this_week_target_km"] == band_for_week(
        example_season(), date.today()
    )["this_week_target_km"]
    rejected = server.update_season("2027", {"constraints": {}}, reason="")
    assert "reason" in rejected["error"]
    updated = server.update_season("2027", {"constraints": {"preferred_training_time": "evening"}}, reason="keep evenings")
    assert updated["ok"] is True
    assert any(note["text"] == "keep evenings" for note in server.get_plan_status()["recent_notes"])
    assert server.get_plan_status()["scaling"]["overridden"] is False


def test_week_start_is_rejected_and_week_of_changes_this_week_target(plan_db) -> None:
    del plan_db
    server.set_season(example_season())
    rejected = server.update_season(
        "2027",
        {"week_overrides": [{"week_start": "2026-11-09", "weekly_km": [25, 30]}]},
        reason="guessed the key",
    )
    assert "week_start" in rejected["error"]
    assert "invalid" in rejected["error"]
    not_monday = server.update_season(
        "2027",
        {"week_overrides": [{"week_of": "2026-11-04", "weekly_km": [12, 16]}]},
        reason="wednesday",
    )
    assert "week_of" in not_monday["error"]
    assert "Monday" in not_monday["error"]
    bad_band = server.update_season(
        "2027",
        {"week_overrides": [{"week_of": "2026-11-09", "weekly_km": 30}]},
        reason="not a band",
    )
    assert "weekly_km" in bad_band["error"]

    from season_plan import week_monday

    monday = week_monday(date.today())
    saved = server.update_season(
        "2027",
        {
            "week_overrides": [
                {
                    "week_of": monday.isoformat(),
                    "weekly_km": [12, 16],
                    "reason": "deload",
                    "hard_pct": 0,
                }
            ]
        },
        reason="cap this week",
    )
    assert saved["ok"] is True
    status = server.get_plan_status()
    assert status["this_week_target"] == [12, 16]
    assert status["scaling"]["overridden"] is True
    assert status["scaling"]["this_week_target_km"] == [12, 16]
    assert status["week_overrides"][0]["week_of"] == monday.isoformat()
    assert status["week_overrides"][0]["resolved_band"] == [12, 16]
    assert status["week_overrides"][0]["overridden"] is True
    stored = server.get_season()
    assert stored["week_overrides"][0]["week_of"] == monday.isoformat()
    assert stored["honoured_overrides"][0]["resolved_band"] == [12, 16]
    assert stored["honoured_overrides"][0]["overridden"] is True


def test_get_week_plan_applies_the_base_override(plan_db) -> None:
    del plan_db
    season = example_season()
    season["week_overrides"] = [
        {"week_of": "2026-11-09", "weekly_km": [30, 40], "reason": "base start"}
    ]
    server.set_season(season)
    with patch.object(server, "_get_client_or_error") as getter:
        plan = server.get_week_plan("2026-11-09")
    getter.assert_not_called()
    assert plan["phase"] == "base"
    assert plan["overridden"] is True
    assert plan["band"] == [30, 40]
    assert plan["override"]["reason"] == "base start"
    assert plan["template"]
    assert [session["date"] for session in plan["sessions"]] == [
        "2026-11-10",
        "2026-11-12",
        "2026-11-15",
    ]
    refused = server.get_week_plan("2026-11-10")
    assert "Monday" in refused["error"]


def test_dry_run_returns_warnings_and_uploads_nothing(plan_db) -> None:
    del plan_db
    server.set_season(example_season())
    session = {
        "date": "2027-01-13",
        "type": "long",
        "intent": "easy",
        "name": "Wed Easy",
        "minutes": 40,
        "zone": 2,
        "steps": [
            {
                "type": "interval",
                "end": {"type": "time", "minutes": 40},
                "target": {"type": "hr_zone", "zone": 2},
            }
        ],
    }
    with patch.object(server, "_get_client_or_error") as getter:
        result = server.schedule_week("2027-01-11", [session], dry_run=True)
    getter.assert_not_called()
    assert any("intent long" in warning for warning in result["warnings"])
    assert result["results"][0]["dry_run"] is True
    assert result["created"] == 0

    season = example_season()
    season["week_overrides"] = [
        {
            "week_of": "2026-11-09",
            "weekly_km": [30, 40],
            "hard_pct": 0,
            "sessions": [
                {
                    "weekday": "TUESDAY",
                    "name": "Hills",
                    "intent": "hard",
                    "minutes": 40,
                    "zone": 4,
                    "type": "intervals",
                }
            ],
        }
    ]
    server.set_season(season)
    with patch.object(server, "_get_client_or_error") as getter:
        plan = server.get_week_plan("2026-11-09")
    getter.assert_not_called()
    assert plan["overridden"] is True
    assert any("hard_pct" in warning for warning in plan["warnings"])
    detail = next(item for item in plan["warning_details"] if item["code"] == "hard_pct_exceeded")
    assert detail["value"] == 100
    assert detail["limit"] == 0


def test_missing_week_band_and_longest_floor(plan_db) -> None:
    season = example_season()
    season["phases"][0].pop("weekly_km")
    server.set_season(season)
    status = server.get_plan_status()
    assert "missing_week_band" in status["warnings"]
    assert status["this_week_target"] is None

    season = example_season()
    season["goals"][0]["min_longest_run_km"] = None
    server.set_season(season)
    from plan_store import save_actuals

    save_actuals(
        "2027",
        {
            "as_of": datetime.now(ZoneInfo("Europe/Brussels")).isoformat(),
            "last_4_week_avg_run_km": 31.8,
            "longest_run_last_28_days_km": 42.92,
            "runs_present": True,
            "other_sport_min": 0,
            "other_activities": [],
            "run_km_by_week": {},
            "covered_weeks": [],
            "events": [],
        },
    )
    quiet = server.get_plan_status()
    assert not any("floor" in warning for warning in quiet["warnings"])
    assert quiet["longest_run_last_28_days_km"] == 42.92

    season["goals"][0]["min_longest_run_km"] = 50
    server.set_season(season)
    warned = server.get_plan_status()
    assert any("floor" in warning for warning in warned["warnings"])


def test_volume_warning_rules() -> None:
    snapshot = {"as_of": "2026-10-28T07:40:00+01:00", "last_4_week_avg_run_km": 31.8, "runs_present": True}
    assert volume_warning(15.4, snapshot, refresh_failed=False) is None
    assert volume_warning(40, snapshot, refresh_failed=False)
    assert volume_warning(40, None, refresh_failed=False) is None
    assert volume_warning(40, snapshot, refresh_failed=True) is None
    bad_zero = {"as_of": "2026-10-28T07:40:00+01:00", "last_4_week_avg_run_km": 0, "runs_present": True}
    assert volume_warning(20, bad_zero, refresh_failed=False) is None
    empty = {"as_of": "2026-10-28T07:40:00+01:00", "last_4_week_avg_run_km": 0, "runs_present": False}
    assert volume_warning(20, empty, refresh_failed=False) is None


def test_warning_carries_value_limit_and_basis() -> None:
    season = example_season()
    season["phases"].insert(
        0,
        {
            "name": "recovery",
            "start": "2026-10-12",
            "end": "2026-10-25",
            "weekly_km": [10, 20],
            "hard_pct": 0,
            "template": [],
        },
    )
    messages, _allowed, details = schedule_warnings(
        season,
        [
            {
                "date": "2026-11-10",
                "name": "Hills",
                "intent": "hard",
                "type": "intervals",
                "minutes": 40,
            }
        ],
        today=date(2026, 11, 10),
        snapshot=None,
        refresh_failed=False,
    )
    hard = next(item for item in details if item["code"] == "hard_pct_exceeded")
    assert hard["message"] in messages
    assert hard["value"] == 100
    assert hard["limit"] == 10
    assert "intent is hard" in hard["basis"]
    assert hard["hint"]

    snapshot = {
        "as_of": "2026-11-10T08:00:00+01:00",
        "last_4_week_avg_run_km": 24.5,
        "runs_present": True,
        "run_km_by_week": {
            "2026-10-05": 40,
            "2026-10-12": 10,
            "2026-10-19": 8,
            "2026-10-26": 8,
        },
    }
    # 10 and 8 km weeks are recovery or return. Leaving them in makes 30 km look
    # like a spike. Excluding them leaves the 40 km week, so 30 km does not warn.
    assert volume_warning(
        30, snapshot, refresh_failed=False, season=season, today=date(2026, 11, 10)
    ) is None
    warned = volume_warning(
        50, snapshot, refresh_failed=False, season=season, today=date(2026, 11, 10)
    )
    assert warned
    _messages, _allowed, volume_details = schedule_warnings(
        season,
        [
            {
                "date": "2026-11-10",
                "name": "Long",
                "intent": "long",
                "type": "long",
                "minutes": 200,
                "distance_km_estimate": 50,
            }
        ],
        today=date(2026, 11, 10),
        snapshot=snapshot,
        refresh_failed=False,
    )
    volume = next(item for item in volume_details if item["code"] == "volume_above_average")
    assert volume["value"] == 50
    assert volume["limit"] == 46
    assert "excluded" in volume["basis"]
    assert volume["hint"]


def test_actuals_window_includes_today_and_race_distance() -> None:
    today = date(2026, 10, 28)
    summary = summarize_activities(
        [
            {
                "activityType": {"typeKey": "running"},
                "distance": 42920,
                "startTimeLocal": "2026-10-18 09:00:00",
                "activityName": "Antwerp Running",
                "movingDuration": 14000,
                "eventType": {"typeKey": "race"},
            },
            {
                "activityType": {"typeKey": "trail_running"},
                "distance": 20000,
                "startTimeLocal": "2026-10-10 09:00:00",
                "activityName": "Trail",
                "movingDuration": 20000,
            },
            {
                "activityType": {"typeKey": "running"},
                "distance": 5000,
                "startTimeLocal": "2026-09-30 09:00:00",
                "activityName": "Before window",
                "movingDuration": 1800,
            },
            {
                "activityType": {"typeKey": "running"},
                "distance": 1000,
                "startTimeLocal": "2026-10-28 08:00:00",
                "activityName": "Today",
                "movingDuration": 400,
            },
            {
                "activityType": {"typeKey": "cycling"},
                "distance": 20000,
                "startTimeLocal": "2026-10-27 18:00:00",
                "activityName": "Commute",
                "movingDuration": 3600,
            },
        ],
        today,
    )
    # 42.92 + 20 + 1.0 today. 30 Sep is outside today-27 days.
    assert summary["last_4_week_avg_run_km"] == pytest.approx((42.92 + 20 + 1.0) / 4, abs=0.05)
    assert summary["longest_run_last_28_days_km"] == pytest.approx(42.92)
    assert summary["other_sport_min"] == 0
    assert summary["other_activities"] == []
    assert summary["last_4_week_avg_training_min"] == pytest.approx(
        (14000 + 20000 + 400 + 3600) / 60 / 4, abs=0.05
    )
    kinds = {row["type"] for row in summary["activities"]}
    assert kinds == {"running", "trail_running", "cycling"}
    assert summary["runs_present"] is True
    assert summary["window"]["end"] == "2026-10-28"
    assert summarize_activities([], today)["last_4_week_avg_run_km"] == 0


def _october_client(items_by_month):
    client = MagicMock()

    def scheduled(year, month):
        return {"calendarItems": items_by_month.get((year, month), [])}

    client.get_scheduled_workouts.side_effect = scheduled
    client.upload_running_workout.return_value = {"workoutId": 999}
    client.schedule_workout.return_value = {"workoutScheduleId": 50}
    client.unschedule_workout.return_value = {"ok": True}
    return client


def _call(client, method, *args, **kwargs):
    return getattr(client, method)(*args, **kwargs)


OCTOBER_ITEMS = {
    (2026, 10): [
        {
            "itemType": "workout",
            "date": "2026-10-28",
            "title": "Wed Easy Z1 (foot test)",
            "workoutId": 1743836620,
            "id": 11,
        },
        {
            "itemType": "workout",
            "date": "2026-10-30",
            "title": "Fri Easy Z1",
            "workoutId": 1743836502,
            "id": 12,
        },
        {"itemType": "event", "date": "2026-10-29", "title": "Not a workout", "id": 3},
        {"itemType": "nap", "date": "2026-10-28", "title": "Nap"},
        {"itemType": "activity", "date": "2026-10-28", "title": "Done", "workoutId": 1},
    ],
    (2026, 11): [
        {
            "itemType": "workout",
            "date": "2026-11-01",
            "title": "Sun Easy Z2",
            "workoutId": 1743836513,
            "id": 13,
        }
    ],
}


def test_schedule_week_adopts_then_is_idempotent(plan_db) -> None:
    del plan_db
    season = example_season()
    server.set_season(season)
    client = _october_client(OCTOBER_ITEMS)
    sessions = export_sessions(resolve_sessions(season, date(2026, 10, 26)))
    first = run_schedule_week(
        season,
        sessions,
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    by_date = {row["date"]: row for row in first["results"]}
    assert by_date["2026-11-01"] == {
        "date": "2026-11-01",
        "ok": True,
        "adopted": True,
        "workout_id": 1743836513,
        "schedule_id": 13,
        "name": "Sun Easy",
    }
    assert by_date["2026-10-28"]["workout_id"] == 1743836620
    assert by_date["2026-10-30"]["workout_id"] == 1743836502
    client.upload_running_workout.assert_not_called()

    second = run_schedule_week(
        season,
        sessions,
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    assert second["created"] == 0
    assert second["skipped"] == 3
    assert all(row.get("skipped") == "duplicate" for row in second["results"])
    assert all(row.get("schedule_id") for row in second["results"])
    client.upload_running_workout.assert_not_called()


def test_schedule_three_list_then_delete_each_schedule_id(plan_db) -> None:
    del plan_db
    season = example_season()
    server.set_season(season)
    items: list[dict] = []
    names: dict[int, str] = {}
    seq = {"n": 0}

    def scheduled(year, month):
        prefix = f"{year:04d}-{month:02d}"
        return {
            "calendarItems": [
                item for item in items if str(item["date"]).startswith(prefix)
            ]
        }

    def upload(workout):
        seq["n"] += 1
        workout_id = 1000 + seq["n"]
        names[workout_id] = workout.workoutName
        return {"workoutId": workout_id, "workoutName": workout.workoutName}

    def schedule(workout_id, day):
        seq["n"] += 1
        schedule_id = 5000 + seq["n"]
        items.append(
            {
                "itemType": "workout",
                "date": day,
                "title": names.get(int(workout_id), "session"),
                "workoutId": int(workout_id),
                "id": schedule_id,
                "sportType": {"sportTypeKey": "running"},
            }
        )
        return {"workoutScheduleId": schedule_id}

    def unschedule(schedule_id):
        items[:] = [item for item in items if item["id"] != int(schedule_id)]
        return {"ok": True}

    client = MagicMock()
    client.get_scheduled_workouts.side_effect = scheduled
    client.upload_running_workout.side_effect = upload
    client.schedule_workout.side_effect = schedule
    client.unschedule_workout.side_effect = unschedule
    sessions = export_sessions(resolve_sessions(season, date(2026, 10, 26)))
    assert len(sessions) == 3
    result = run_schedule_week(
        season,
        sessions,
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 26),
    )
    assert len(result["results"]) == 3
    assert all(row.get("ok") for row in result["results"])
    assert all(row.get("schedule_id") for row in result["results"])
    assert all(row.get("name") for row in result["results"])

    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        listed = server.list_scheduled_workouts("2026-10-26", "2026-11-01")
        assert {row["schedule_id"] for row in listed} == {
            row["schedule_id"] for row in result["results"]
        }
        assert all(row["sport"] == "running" for row in listed)
        assert all("source" not in row for row in listed)
        for row in result["results"]:
            deleted = server.delete_scheduled_workout(schedule_id=row["schedule_id"])
            assert deleted["ok"] is True
        assert server.list_scheduled_workouts("2026-10-26", "2026-11-01") == []


def test_schedule_week_partial_retry_adopts_only_the_missing_dates(plan_db) -> None:
    del plan_db
    season = example_season()
    save_schedule_record(
        idempotency_key="2026-10-26|2026-10-28|wed-easy-z1-foot-test",
        date="2026-10-28",
        workout_id=1743836620,
        schedule_id=11,
    )
    client = _october_client(OCTOBER_ITEMS)
    sessions = [
        {
            "date": "2026-10-28",
            "type": "easy",
            "intent": "easy",
            "name": "Wed Easy Z1 (foot test)",
            "minutes": 30,
            "zone": 1,
            "idempotency_key": "2026-10-26|2026-10-28|wed-easy-z1-foot-test",
            "steps": [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 30},
                    "target": {"type": "hr_zone", "zone": 1},
                }
            ],
        },
        {
            "date": "2026-10-30",
            "type": "easy",
            "intent": "easy",
            "name": "Fri Easy Z1",
            "minutes": 30,
            "zone": 1,
            "steps": [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 30},
                    "target": {"type": "hr_zone", "zone": 1},
                }
            ],
        },
    ]
    result = run_schedule_week(
        season,
        sessions,
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    rows = {row["date"]: row for row in result["results"]}
    assert rows["2026-10-28"]["skipped"] == "duplicate"
    assert rows["2026-10-30"]["adopted"] is True
    assert rows["2026-10-30"]["workout_id"] == 1743836502
    client.upload_running_workout.assert_not_called()


def test_same_library_workout_on_a_new_date_is_still_adopted(plan_db) -> None:
    del plan_db
    season = example_season()
    save_schedule_record(
        idempotency_key="2026-10-19|2026-10-21|old",
        date="2026-10-21",
        workout_id=1743836502,
        schedule_id=1,
    )
    client = MagicMock()
    client.get_scheduled_workouts.return_value = {
        "calendarItems": [
            {
                "itemType": "workout",
                "date": "2026-10-30",
                "title": "Fri Easy Z1",
                "workoutId": 1743836502,
                "id": 99,
            }
        ]
    }
    result = run_schedule_week(
        season,
        [
            {
                "date": "2026-10-30",
                "type": "easy",
                "intent": "easy",
                "name": "Fri Easy Z1",
                "minutes": 30,
                "zone": 1,
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "time", "minutes": 30},
                        "target": {"type": "hr_zone", "zone": 1},
                    }
                ],
            }
        ],
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    assert result["results"][0]["adopted"] is True
    assert result["results"][0]["workout_id"] == 1743836502
    client.upload_running_workout.assert_not_called()


def test_delete_scheduled_workout_forgets_the_idempotency_row(plan_db) -> None:
    del plan_db
    season = example_season()
    server.set_season(season)
    save_schedule_record(
        idempotency_key="2026-10-26|2026-10-28|wed-easy-z1-foot-test",
        date="2026-10-28",
        workout_id=1743836620,
        schedule_id=11,
    )
    client = MagicMock()
    client.unschedule_workout.return_value = {"ok": True}
    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        deleted = server.delete_scheduled_workout(schedule_id=11, workout_date="2026-10-28")
    assert deleted["ok"] is True
    assert (
        get_schedule_record("2026-10-26|2026-10-28|wed-easy-z1-foot-test") is None
    )


def test_delete_workout_lists_calendar_rows_that_remain(plan_db) -> None:
    del plan_db
    items = {
        (2026, 11): [
            {
                "itemType": "workout",
                "date": "2026-11-11",
                "title": "Wed Vert",
                "workoutId": 55,
                "id": 1829,
            }
        ]
    }
    client = _october_client(items)
    client.delete_workout.return_value = {"ok": True}
    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        deleted = server.delete_workout(55)
    assert deleted["ok"] is True
    assert deleted["workout_id"] == 55
    assert deleted["calendar_rows"] == [
        {"schedule_id": 1829, "date": "2026-11-11", "name": "Wed Vert"}
    ]
    assert "cleared" not in str(deleted).lower()


def test_delete_scheduled_workout_names_the_row_and_expect_name_blocks(plan_db) -> None:
    del plan_db
    items = {
        (2026, 11): [
            {
                "itemType": "workout",
                "date": "2026-11-11",
                "title": "Wed Vert",
                "workoutId": 55,
                "id": 1829382547,
            },
            {
                "itemType": "workout",
                "date": "2026-11-05",
                "title": "Thu commute",
                "workoutId": 56,
                "id": 99,
            },
        ]
    }
    client = _october_client(items)
    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        blocked = server.delete_scheduled_workout(
            schedule_id=1829382547,
            workout_id=56,
            workout_date="2026-11-05",
            expect_name="Thu commute",
        )
    assert blocked["ok"] is False
    assert "error" in blocked
    assert blocked["schedule_id"] == 1829382547
    assert blocked["workout_id"] == 55
    assert blocked["name"] == "Wed Vert"
    assert blocked["date"] == "2026-11-11"
    client.unschedule_workout.assert_not_called()

    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        deleted = server.delete_scheduled_workout(
            schedule_id=1829382547,
            expect_name="Wed Vert",
        )
    assert deleted == {
        "ok": True,
        "schedule_id": 1829382547,
        "workout_id": 55,
        "name": "Wed Vert",
        "date": "2026-11-11",
    }
    client.unschedule_workout.assert_called_once_with(1829382547)


def test_failed_schedule_retries_without_a_second_upload(plan_db) -> None:
    del plan_db
    season = example_season()
    client = _october_client({})
    client.schedule_workout.side_effect = [
        {"error": "schedule failed"},
        {"workoutScheduleId": 77},
    ]
    session = {
        "date": "2026-10-28",
        "type": "easy",
        "intent": "easy",
        "name": "Wed Easy",
        "minutes": 30,
        "zone": 1,
        "steps": [
            {
                "type": "interval",
                "end": {"type": "time", "minutes": 30},
                "target": {"type": "hr_zone", "zone": 1},
            }
        ],
    }
    first = run_schedule_week(
        season,
        [session],
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    assert first["results"][0]["ok"] is False
    assert first["results"][0]["name"] == "Wed Easy"
    assert first["results"][0]["workout_id"] == 999
    assert "schedule_id" not in first["results"][0]
    second = run_schedule_week(
        season,
        [session],
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        real_today=date(2026, 10, 28),
    )
    assert second["results"][0]["ok"] is True
    assert second["results"][0]["workout_id"] == 999
    assert second["results"][0]["schedule_id"] == 77
    assert second["results"][0]["name"] == "Wed Easy"
    assert client.upload_running_workout.call_count == 1
    assert client.schedule_workout.call_count == 2


def test_week_that_overlaps_a_phase_keeps_the_band() -> None:
    season = {
        "season_id": "overlap",
        "timezone": "Europe/Brussels",
        "goals": [],
        "phases": [
            {
                "name": "return_to_run",
                "start": "2026-10-28",
                "end": "2026-11-08",
                "weekly_km": [10, 25],
                "hard_pct": 0,
            }
        ],
    }
    band = band_for_week(season, date(2026, 10, 26))
    assert band["phase"]["name"] == "return_to_run"
    assert band["this_week_target_km"] == [10, 18]
    assert resolve_sessions(season, date(2026, 10, 26))


def test_combine_uploads_the_normalized_pace_step(monkeypatch) -> None:
    captured: dict = {}

    def upload(client, workout):
        del client
        captured["workout"] = workout
        return {"workoutId": 1}

    monkeypatch.setattr(server, "_get_client_or_error", lambda: (object(), None))
    monkeypatch.setattr(server, "_upload_running_workout", upload)
    monkeypatch.setattr(server, "get_profile", lambda: {})
    monkeypatch.setattr(server, "resolve_hr_context", lambda *_args, **_kwargs: {"zones": []})
    result = server.combine_workout_templates(
        "pace",
        [
            {
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "time", "minutes": 24},
                        "target": {"type": "pace", "mps_low": 2.6, "mps_high": 2.78},
                    }
                ]
            }
        ],
    )
    assert result["resolved_steps"][0]["target"]["type"] == "pace"
    segment = captured["workout"].workoutSegments[0]
    step = segment.workoutSteps[0]
    assert step.targetValueOne == 2.6
    assert step.targetValueTwo == 2.78


def test_stale_actuals_do_not_warn_on_schedule(plan_db) -> None:
    del plan_db
    season = example_season()
    server.set_season(season)
    save_actuals(
        season["season_id"],
        {
            "as_of": "2026-10-01T00:00:00+02:00",
            "last_4_week_avg_run_km": 10,
            "runs_present": True,
        },
    )
    result = server.schedule_week(
        "2026-10-26",
        [
            {
                "date": "2026-10-28",
                "type": "easy",
                "intent": "easy",
                "name": "Long easy",
                "minutes": 180,
                "zone": 2,
                "steps": [
                    {
                        "type": "interval",
                        "end": {"type": "time", "minutes": 180},
                        "target": {"type": "hr_zone", "zone": 2},
                    }
                ],
            }
        ],
        dry_run=True,
    )
    assert not any("15%" in item for item in result["warnings"])


def test_matching_race_date_does_not_suggest_moving_it() -> None:
    actions = what_you_can_do(
        has_sessions=True,
        mismatch=None,
        goal_date="2027-04-24",
        race_on_calendar=True,
    )
    assert all("wrong date" not in item["when"] for item in actions)
    assert all("remove_event" not in item["tool"] for item in actions)


def test_two_unrecorded_workouts_are_not_adopted_or_uploaded(plan_db) -> None:
    del plan_db
    season = example_season()
    items = {
        (2026, 11): [
            {"itemType": "workout", "date": "2026-11-01", "title": "Sun Easy Z2", "workoutId": 1, "id": 1},
            {"itemType": "workout", "date": "2026-11-01", "title": "Other", "workoutId": 2, "id": 2},
        ]
    }
    client = _october_client(items)
    sessions = [
        {
            "date": "2026-11-01",
            "type": "long",
            "intent": "easy",
            "name": "Sun Easy",
            "minutes": 40,
            "zone": 2,
            "steps": [
                {
                    "type": "interval",
                    "end": {"type": "time", "minutes": 40},
                    "target": {"type": "hr_zone", "zone": 2},
                }
            ],
        }
    ]
    result = run_schedule_week(
        season,
        sessions,
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        replace_existing=True,
        real_today=date(2026, 10, 28),
    )
    assert result["results"][0]["ok"] is False
    assert "ambiguous" in result["results"][0]["warning"] or "more than one" in result["results"][0]["warning"]
    client.upload_running_workout.assert_not_called()
    client.unschedule_workout.assert_not_called()


def test_replace_existing_does_not_delete_unrecorded_until_adopted(plan_db) -> None:
    del plan_db
    season = example_season()
    store = {(2026, 11): [
        {"itemType": "workout", "date": "2026-11-01", "title": "Sun Easy Z2", "workoutId": 1743836513, "id": 13}
    ]}
    client = _october_client(store)

    def unschedule(schedule_id):
        store[(2026, 11)].clear()
        return {"ok": True, "schedule_id": schedule_id}

    client.unschedule_workout.side_effect = unschedule
    session = {
        "date": "2026-11-01",
        "type": "long",
        "intent": "easy",
        "name": "Sun Easy",
        "minutes": 40,
        "zone": 2,
        "idempotency_key": "2026-10-26|2026-11-01|sun-easy",
        "steps": [
            {
                "type": "interval",
                "end": {"type": "time", "minutes": 40},
                "target": {"type": "hr_zone", "zone": 2},
            }
        ],
    }
    first = run_schedule_week(
        season,
        [session],
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        replace_existing=True,
        real_today=date(2026, 10, 28),
    )
    assert first["results"][0]["adopted"] is True
    client.unschedule_workout.assert_not_called()
    client.upload_running_workout.assert_not_called()

    second = run_schedule_week(
        season,
        [session],
        client=client,
        call_optional=_call,
        week_start="2026-10-26",
        replace_existing=True,
        real_today=date(2026, 10, 28),
    )
    client.unschedule_workout.assert_called_once()
    client.upload_running_workout.assert_called_once()
    assert second["results"][0]["ok"] is True
    assert second["results"][0].get("adopted") is not True


def test_sync_block_default_deletes_nothing_and_caps_at_four_weeks(plan_db) -> None:
    del plan_db
    season = example_season()
    client = _october_client({})
    result = run_sync_block(
        season,
        client=client,
        call_optional=_call,
        today=date(2026, 10, 28),
        weeks_ahead=10,
        replace_existing=False,
    )
    assert len(result["weeks"]) == 4
    assert any("did not delete" in warning for warning in result["warnings"])
    client.unschedule_workout.assert_not_called()


def test_build_week_without_intent_long_warns_even_if_type_is_long(plan_db) -> None:
    del plan_db
    season = example_season()
    session = {
        "date": "2027-01-13",
        "type": "long",
        "intent": "easy",
        "name": "Wed Easy",
        "minutes": 40,
        "zone": 2,
        "steps": [
            {
                "type": "interval",
                "end": {"type": "time", "minutes": 40},
                "target": {"type": "hr_zone", "zone": 2},
            }
        ],
    }
    result = run_schedule_week(
        season,
        [session],
        client=MagicMock(),
        call_optional=_call,
        week_start="2027-01-11",
        dry_run=True,
        real_today=date(2027, 1, 13),
    )
    assert any("intent long" in warning for warning in result["warnings"])
    assert result["results"][0]["dry_run"] is True


def test_call_optional_retries_reads_and_not_a_landed_write() -> None:
    client = MagicMock()
    client.get_user_profile.side_effect = [ConnectionResetError("reset"), {"displayName": "A"}]
    assert server._call_optional(client, "get_user_profile") == {"displayName": "A"}
    assert client.get_user_profile.call_count == 2

    client.schedule_workout.side_effect = ConnectionResetError("reset")
    client.get_scheduled_workouts.return_value = {
        "calendarItems": [{"date": "2026-10-28", "workoutId": 5, "itemType": "workout"}]
    }
    landed = server._call_optional(client, "schedule_workout", 5, "2026-10-28")
    assert landed["workoutId"] == 5
    assert client.schedule_workout.call_count == 1

    client.schedule_workout.side_effect = ConnectionResetError("reset")
    client.get_scheduled_workouts.return_value = {"calendarItems": []}
    missed = server._call_optional(client, "schedule_workout", 6, "2026-10-29")
    assert "failed" in missed["error"]
    assert client.schedule_workout.call_count == 2

    client.get_user_profile.side_effect = GarminConnectAuthenticationError("nope")
    client.get_user_profile.reset_mock()
    failed = server._call_optional(client, "get_user_profile")
    assert "failed" in failed["error"]
    assert client.get_user_profile.call_count == 1


def test_goal_inside_no_attempt_window_warns_on_status() -> None:
    season = example_season()
    season["goals"].append(
        {
            "goal_id": "november_fkt",
            "title": "November FKT",
            "date": "2026-11-20",
            "priority": "A",
        }
    )
    warnings = status_warnings(
        season,
        date(2026, 10, 28),
        None,
        refresh_failed=False,
        sessions=[
            {
                "date": "2026-10-28",
                "name": "Wed Easy Z1 (foot test)",
                "intent": "easy",
                "type": "easy",
                "minutes": 30,
            }
        ],
    )
    assert any("November FKT" in item and "no-attempt" in item for item in warnings)
    assert not any("Wed Easy" in item for item in warnings)


def test_passed_ab_goal_replaces_older_last_race() -> None:
    season = example_season()
    season["goals"][0]["date"] = "2026-10-20"
    assert promote_last_race(season, date(2026, 10, 28)) is True
    assert season["last_race"]["date"] == "2026-10-20"
    assert season["last_race"]["goal_id"] == "pacte_siberien"
    season["goals"][0]["priority"] = "C"
    season["goals"][0]["date"] = "2026-10-27"
    season["last_race"] = {"title": "Antwerp Marathon", "date": "2026-10-18"}
    assert promote_last_race(season, date(2026, 10, 28)) is False
    assert season["last_race"]["date"] == "2026-10-18"


def test_refresh_rate_limit_keeps_the_overview(plan_db) -> None:
    del plan_db
    season = example_season()
    server.set_season(season)
    snapshot = {
        "as_of": "2026-10-01T08:00:00+02:00",
        "last_4_week_avg_run_km": 29.8,
        "longest_run_last_28_days_km": 42.92,
        "runs_present": True,
        "other_sport_min": 41.4,
        "other_activities": [],
        "events": [],
    }

    def limited(*_args, **_kwargs):
        raise RuntimeError("429 too many requests")

    payload = build_coaching_overview(
        object(),
        limited,
        season=season,
        brief={"coaching_brief": {"next_week_proposal": {"target_min": 10, "focus": "no"}}},
        now=datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("Europe/Brussels")),
        snapshot=snapshot,
    )
    assert payload["sessions"]
    assert payload["recent_training"]["actuals_as_of"] == "2026-10-01T08:00:00+02:00"
    assert payload["recent_training"]["days_since_race"] == 10
    assert any("not refreshed" in item for item in payload["warnings"])
    assert not any("15%" in item for item in payload["warnings"])


def test_rate_limit_is_not_retried() -> None:
    client = MagicMock()
    client.get_user_profile.side_effect = ConnectionResetError("429 too many requests")
    failed = server._call_optional(client, "get_user_profile")
    assert "429" in failed["error"]
    assert client.get_user_profile.call_count == 1


def test_retryable_login_failure_does_not_stick() -> None:
    from errors import CONNECTION_ERROR, structured_error

    server._CLIENT = None
    server._CLIENT_ERROR = structured_error(CONNECTION_ERROR, "reset", retryable=True)
    mock_client = MagicMock()
    with (
        patch.object(server, "DEFAULT_GARMIN_USERNAME", "user@example.com"),
        patch.object(server, "DEFAULT_GARMIN_PASSWORD", "secret"),
        patch.object(server, "Garmin", return_value=mock_client),
    ):
        client, error = server._get_client_or_error()
    assert error is None
    assert client is mock_client
    server._CLIENT = None
    server._CLIENT_ERROR = None


def _overview_calendar(client, method, *args, **kwargs):
    del client
    if method != "get_scheduled_workouts":
        raise AssertionError(method)
    year = kwargs.get("year")
    month = kwargs.get("month")
    if (year, month) == (2026, 10):
        return {
            "calendarItems": [
                {
                    "itemType": "workout",
                    "date": "2026-10-28",
                    "title": "Wed Easy Z1 (foot test)",
                    "workoutId": 1743836620,
                },
                {
                    "itemType": "workout",
                    "date": "2026-10-30",
                    "title": "Fri Easy Z1",
                    "workoutId": 1743836502,
                },
                {"itemType": "activity", "date": "2026-10-28", "title": "Activity"},
                {"itemType": "nap", "date": "2026-10-29", "title": "Nap"},
                {"itemType": "event", "date": "2026-10-20", "title": "Past", "id": 4},
            ]
        }
    if (year, month) == (2026, 11):
        return {
            "calendarItems": [
                {
                    "itemType": "workout",
                    "date": "2026-11-01",
                    "title": "Sun Easy Z2",
                    "workoutId": 1743836513,
                }
            ]
        }
    if (year, month) == (2027, 4):
        return {
            "calendarItems": [
                {
                    "itemType": "event",
                    "date": "2027-04-23",
                    "title": "Pacte Sibérien",
                    "id": 88121,
                }
            ]
        }
    return {"calendarItems": []}


def test_october_28_overview_shape() -> None:
    season = example_season()
    season["phases"].insert(
        0,
        {
            "name": "marathon_week",
            "start": "2026-10-19",
            "end": "2026-10-25",
            "weekly_km": [40, 60],
            "hard_pct": 0,
            "key_sessions": [],
            "template": [],
        },
    )
    now = datetime(2026, 10, 28, 7, 40, tzinfo=ZoneInfo("Europe/Brussels"))
    snapshot = {
        "as_of": "2026-10-28T07:40:00+01:00",
        "last_4_week_avg_run_km": 31.8,
        "longest_run_last_28_days_km": 42.92,
        "other_sport_min": 60,
        "other_activities": [
            {"date": "2026-10-27", "type": "cycling", "name": "Commute", "minutes": 60}
        ],
        "run_km_by_week": {"2026-10-19": 10},
        "covered_weeks": ["2026-10-19"],
        "runs_present": True,
        "events": [{"event_id": 88121, "title": "Pacte Sibérien", "date": "2027-04-23"}],
    }
    notes = [
        {
            "date": "2026-10-27",
            "scope": "week",
            "key": "2026-10-26",
            "text": "Left foot dorsal + plantar pain post-marathon. Retired Escalante 4 (800km), tight lacing. Return limited to Z1, foot test Wednesday.",
            "tags": ["injury", "foot", "equipment"],
        }
    ]
    brief = {
        "coaching_brief": {
            "next_week_proposal": {
                "week_type": "build",
                "target_min": 323,
                "focus": "sprints",
                "sessions": [{"duration_minutes": 30}],
            }
        }
    }
    payload = build_coaching_overview(
        object(),
        _overview_calendar,
        season=season,
        brief=brief,
        now=now,
        snapshot=snapshot,
        notes=notes,
    )
    assert payload["today"] == "2026-10-28"
    assert payload["timezone"] == "Europe/Brussels"
    where = payload["where_we_are"]
    assert where["phase"] == "return_to_run"
    assert where["week_in_phase"] == 1
    assert where["phase_weeks_total"] == 2
    assert where["days_to_goal"] == 178
    assert where["days_to_next_goal"] == 241
    assert where["later_goals"][0]["goal_id"] == "uths_100"
    assert where["later_goals"][0]["days_to_goal"] == 277
    assert where["current_goal"]["min_longest_run_km"] is None
    assert payload["scaling"]["phase_weekly_km"] == [10, 25]
    assert payload["scaling"]["this_week_target_km"] == [10, 18]
    assert payload["scaling"]["overridden"] is False
    assert payload["scaling"]["override_reason"] is None
    assert payload["scaling"]["hard_pct"] == 0
    assert payload["scaling"]["week_km_estimate"] == 15.4
    assert [session["name"] for session in payload["sessions"]] == [
        "Wed Easy Z1 (foot test)",
        "Fri Easy Z1",
        "Sun Easy",
    ]
    assert payload["sessions"][2]["intent"] == "easy"
    assert payload["sessions"][2]["type"] == "long"
    assert payload["sessions"][2]["idempotency_key"] == "2026-10-26|2026-11-01|sun-easy"
    assert payload["session_policy"]["now"] == []
    assert payload["session_policy"]["later"][0]["phase"] == "base"
    training = payload["recent_training"]
    assert training["actuals_as_of"] == "2026-10-28T07:40:00+01:00"
    assert training["last_4_week_avg_run_km"] == 31.8
    assert training["longest_run_last_28_days_km"] == 42.92
    assert training["days_since_race"] == 10
    assert training["days_since_hard_session"] is None
    assert training["other_sport_min"] == 60
    assert training["adherence"] == {
        "weeks_on_target": 0,
        "weeks_missed": 1,
        "weeks_over": 0,
        "weeks_unknown": 1,
        "evaluated_weeks": 1,
    }
    assert "no_attempt_windows" in payload["constraints"]
    assert "blocked_windows" not in payload["constraints"]
    assert not any("blocked window" in item for item in payload["warnings"])
    assert payload["calendar"]["scheduled_workouts"] == [
        {"date": "2026-10-28", "name": "Wed Easy Z1 (foot test)", "workout_id": 1743836620},
        {"date": "2026-10-30", "name": "Fri Easy Z1", "workout_id": 1743836502},
        {"date": "2026-11-01", "name": "Sun Easy Z2", "workout_id": 1743836513},
    ]
    assert payload["calendar"]["events"] == [
        {
            "event_id": 88121,
            "title": "Pacte Sibérien",
            "date": "2027-04-23",
            "target": {"value": None, "unit": None, "unitType": None},
        }
    ]
    assert payload["warnings"] == [
        "Garmin lists Pacte Sibérien on 2027-04-23. The season date is 2027-04-24."
    ]
    proposal = payload["load_context"]["recent_load_proposal"]
    assert "next_week_proposal" not in payload["load_context"]
    assert "focus" not in proposal
    assert "week_type" not in proposal
    assert proposal == {
        "load": "build",
        "target_min": 323,
    }
    tools = {item["tool"] for item in payload["what_you_can_do"]}
    assert "schedule_week" in tools
    assert "create_workout" in tools
    assert any("2027-04-24" in item["when"] for item in payload["what_you_can_do"])
    assert "start_date" not in server.ACTIVITY_HEADERS


def test_add_event_returns_existing_and_errors_when_the_write_does_not_stick() -> None:
    stored = {
        "calendarItems": [
            {
                "itemType": "event",
                "id": 88121,
                "date": "2027-04-23",
                "title": "Pacte Sibérien",
                "completionTarget": {"value": 100, "unit": "km", "unitType": "distance"},
                "arg3": {"eventName": None, "name": None, "location": "Ghent"},
            }
        ]
    }

    def call(client, method, *args, **kwargs):
        del client, args
        if method == "get_scheduled_workouts":
            return stored
        raise AssertionError(method)

    client = MagicMock()
    client.client.post.return_value = {"id": 1}
    existing = server.add_event.__wrapped__ if hasattr(server.add_event, "__wrapped__") else None
    del existing
    from calendar_events import add_event

    again = add_event(
        client,
        call,
        title="Pacte Sibérien",
        event_date="2027-04-23",
    )
    assert again["created"] is False
    assert again["event"]["event_id"] == 88121
    client.client.post.assert_not_called()

    created = add_event(client, call, title="Pacte Sibérien", event_date="2027-04-24")
    assert "did not stick" in created["error"]
    client.client.post.assert_called_once()
    sent = client.client.post.call_args.kwargs["json"]
    assert sent["eventName"] == "Pacte Sibérien"
    assert sent["title"] == "Pacte Sibérien"
    assert sent["arg3"]["eventName"] == "Pacte Sibérien"
    assert sent["arg3"]["name"] == "Pacte Sibérien"
    assert sent["arg3"]["location"] == "Ghent"
    timed = add_event(
        client,
        call,
        title="Pacte Sibérien",
        event_date="2027-04-25",
        duration_minutes=720,
    )
    assert "did not stick" in timed["error"]
    timed_body = client.client.post.call_args.kwargs["json"]
    assert timed_body["completionTarget"] == {
        "value": 12.0,
        "unit": "hour",
        "unitType": "duration",
    }
    assert timed_body["arg3"]["eventName"] == "Pacte Sibérien"


def test_get_events_shows_title_and_completion_target() -> None:
    stored = {
        "calendarItems": [
            {
                "itemType": "event",
                "id": 42,
                "date": "2027-04-24",
                "title": "Pacte Sibérien",
                "completionTarget": {"value": 53.0, "unit": "km", "unitType": "distance"},
            }
        ]
    }

    def call(client, method, *args, **kwargs):
        del client, args
        if method == "get_scheduled_workouts":
            return stored
        raise AssertionError(method)

    client = MagicMock()
    client.get_scheduled_workouts.return_value = stored
    from calendar_events import events_between

    rows = events_between(client, call, date(2027, 4, 1), date(2027, 4, 30))
    assert rows == [
        {
            "event_id": 42,
            "title": "Pacte Sibérien",
            "date": "2027-04-24",
            "target": {"value": 53.0, "unit": "km", "unitType": "distance"},
        }
    ]
    with patch.object(server, "_get_client_or_error", return_value=(client, None)):
        listed = server.get_events()
    match = next(row for row in listed["events"] if row["title"] == "Pacte Sibérien")
    assert match["target"] == {"value": 53.0, "unit": "km", "unitType": "distance"}


def test_update_event_duration_is_visible_on_the_following_read() -> None:
    stored = {
        "calendarItems": [
            {
                "itemType": "event",
                "id": 7,
                "date": "2027-04-24",
                "title": "Pacte Sibérien",
                "eventName": "Pacte Sibérien",
                "completionTarget": {"value": 53.0, "unit": "km", "unitType": "distance"},
                "arg3": {"eventName": None},
            }
        ]
    }

    def call(client, method, *args, **kwargs):
        del client, args
        if method == "get_scheduled_workouts":
            return stored
        raise AssertionError(method)

    def put(domain, path, json=None, api=None):
        del domain, api
        assert path.endswith("/7")
        assert json["eventName"] == "Pacte Sibérien"
        assert json["arg3"]["eventName"] == "Pacte Sibérien"
        item = stored["calendarItems"][0]
        item["title"] = json["title"]
        item["eventName"] = json["eventName"]
        item["date"] = json["date"]
        item["completionTarget"] = json["completionTarget"]
        item["arg3"] = json["arg3"]
        return {"ok": True}

    client = MagicMock()
    client.client.put.side_effect = put
    from calendar_events import get_event, update_event

    updated = update_event(
        client,
        call,
        event_id=7,
        duration_minutes=12 * 60,
    )
    assert updated["ok"] is True
    assert updated["event"]["title"] == "Pacte Sibérien"
    assert updated["event"]["target"] == {
        "value": 12.0,
        "unit": "hour",
        "unitType": "duration",
    }
    again = get_event(client, call, 7)
    assert again["event"]["target"]["value"] == 12.0
    assert again["event"]["target"]["unitType"] == "duration"

    client.client.put.side_effect = RuntimeError("400 eventName")
    failed = update_event(client, call, event_id=7, title="Renamed")
    assert failed.get("ok") is not True
    assert "failed" in failed["error"]
    assert get_event(client, call, 7)["event"]["title"] == "Pacte Sibérien"

    def put_without_sticking(domain, path, json=None, api=None):
        del domain, path, json, api
        return {"ok": True}

    client.client.put.side_effect = put_without_sticking
    missed = update_event(client, call, event_id=7, title="Still wrong")
    assert missed.get("ok") is not True
    assert "did not stick" in missed["error"]


def test_goal_event_target_mismatch_reports_both_values(plan_db) -> None:
    del plan_db
    from season_plan import goal_target_warnings

    season = example_season()
    season["goals"][0]["garmin_event_id"] = 7
    season["goals"][0]["distance_meters"] = 80470
    events = [
        {
            "event_id": 7,
            "title": "Sibérien",
            "date": "2027-04-24",
            "target": {"value": 53.0, "unit": "km", "unitType": "distance"},
        },
        {
            "event_id": 8,
            "title": "Lee Trail",
            "date": "2027-06-26",
            "target": {"value": 53.0, "unit": "km", "unitType": "distance"},
        },
        {
            "event_id": 9,
            "title": "Pacte Sibérien",
            "date": "2027-04-23",
            "target": {"value": 100, "unit": "km", "unitType": "distance"},
        },
    ]
    details = goal_target_warnings(season, events, today=date(2026, 11, 4))
    mismatch = next(item for item in details if item["code"] == "goal_target_mismatch")
    assert "53 km" in mismatch["message"]
    assert "80.47 km" in mismatch["message"]
    assert mismatch["value"] != mismatch["limit"]
    assert "Sibérien" in mismatch["message"]
    shared = next(item for item in details if item["code"] == "shared_event_target")
    assert "Lee Trail" in shared["message"]
    assert "53 km" in shared["message"]
    # The id is preferred, so the title-matched 100 km event is not the comparison.
    assert "100 km" not in mismatch["message"]

    server.set_season(season)
    now = datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo("Europe/Brussels"))
    payload = build_coaching_overview(
        object(),
        lambda *_args, **_kwargs: {"calendarItems": []},
        season=season,
        brief={},
        now=now,
        snapshot={
            "as_of": "2026-11-04T08:00:00+01:00",
            "runs_present": False,
            "events": events,
        },
        notes=[],
    )
    assert any("53 km" in item and "80.47 km" in item for item in payload["warnings"])
    rejected = server.update_season(
        "2027",
        {"goals": [{**season["goals"][0], "distance_meters": "far"}]},
        reason="bad distance",
    )
    assert "distance_meters" in rejected["error"]


def test_accent_folded_garmin_title_still_flags_the_date_mismatch() -> None:
    from season_plan import garmin_date_mismatch

    message = garmin_date_mismatch(
        example_season(),
        [{"title": "TDLS BACKYARD - LE PACTE SIBERIEN", "date": "2027-04-23", "event_id": 1}],
    )
    assert message == (
        "Garmin lists TDLS BACKYARD - LE PACTE SIBERIEN on 2027-04-23. "
        "The season date is 2027-04-24."
    )


def test_plan_status_does_not_call_garmin(plan_db) -> None:
    del plan_db
    server.set_season(example_season())
    with patch.object(server, "_get_client_or_error") as getter:
        status = server.get_plan_status()
    getter.assert_not_called()
    assert status["current_phase"] == "return_to_run"
    assert status["key_sessions_later"][2]["phase"] == "peak"
