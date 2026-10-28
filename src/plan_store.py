"""Local season database. Garmin is not a copy of the plan."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def db_path() -> Path:
    raw = os.environ.get("GARMIN_PLAN_DB")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share" / "garmin-running-coach-mcp" / "plan.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seasons (
            season_id TEXT PRIMARY KEY,
            document TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id TEXT NOT NULL,
            date TEXT NOT NULL,
            scope TEXT NOT NULL,
            key TEXT NOT NULL,
            text TEXT NOT NULL,
            tags TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schedule_records (
            idempotency_key TEXT PRIMARY KEY,
            season_id TEXT,
            date TEXT NOT NULL,
            workout_id INTEGER NOT NULL,
            schedule_id INTEGER,
            recorded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS actuals (
            season_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_season(document: dict[str, Any]) -> dict[str, Any]:
    season_id = document.get("season_id")
    if not season_id:
        raise ValueError("season_id is required.")
    if not document.get("timezone"):
        raise ValueError("timezone is required.")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO seasons (season_id, document, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
                document = excluded.document,
                updated_at = excluded.updated_at
            """,
            (str(season_id), json.dumps(document), _now()),
        )
    return document


def load_season(season_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    with connect() as conn:
        if season_id:
            row = conn.execute(
                "SELECT document FROM seasons WHERE season_id = ?",
                (season_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT document FROM seasons ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
    if row is None:
        return None
    return json.loads(row["document"])


def add_note(
    season_id: str,
    *,
    date: str,
    scope: str,
    key: str,
    text: str,
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    if scope not in {"week", "phase", "season"}:
        raise ValueError("scope must be week, phase, or season.")
    note = {
        "date": date,
        "scope": scope,
        "key": key,
        "text": text,
        "tags": list(tags or []),
    }
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO notes (season_id, date, scope, key, text, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                season_id,
                date,
                scope,
                key,
                text,
                json.dumps(note["tags"]),
                _now(),
            ),
        )
    return note


def recent_notes(season_id: str, limit: int = 10) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT date, scope, key, text, tags, created_at
            FROM notes
            WHERE season_id = ?
            ORDER BY date DESC, id DESC
            LIMIT ?
            """,
            (season_id, limit),
        ).fetchall()
    notes = []
    for row in rows:
        notes.append(
            {
                "date": row["date"],
                "scope": row["scope"],
                "key": row["key"],
                "text": row["text"],
                "tags": json.loads(row["tags"]),
            }
        )
    return notes


def get_schedule_record(idempotency_key: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM schedule_records WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def recorded_workout_ids() -> set[int]:
    with connect() as conn:
        rows = conn.execute("SELECT workout_id FROM schedule_records").fetchall()
    return {int(row["workout_id"]) for row in rows}


def save_schedule_record(
    *,
    idempotency_key: str,
    date: str,
    workout_id: int,
    schedule_id: Optional[int] = None,
    season_id: Optional[str] = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO schedule_records (
                idempotency_key, season_id, date, workout_id, schedule_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(idempotency_key) DO UPDATE SET
                workout_id = excluded.workout_id,
                schedule_id = excluded.schedule_id,
                date = excluded.date,
                recorded_at = excluded.recorded_at
            """,
            (idempotency_key, season_id, date, int(workout_id), schedule_id, _now()),
        )


def delete_schedule_record(idempotency_key: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM schedule_records WHERE idempotency_key = ?",
            (idempotency_key,),
        )


def records_on_date(date: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM schedule_records WHERE date = ?",
            (date,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_actuals(season_id: str, payload: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO actuals (season_id, payload) VALUES (?, ?)
            ON CONFLICT(season_id) DO UPDATE SET payload = excluded.payload
            """,
            (season_id, json.dumps(payload)),
        )


def load_actuals(season_id: str) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT payload FROM actuals WHERE season_id = ?",
            (season_id,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def merge_patch(document: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    base = document if isinstance(document, dict) else {}
    merged = dict(base)
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_patch(merged[key], value)
        else:
            merged[key] = value
    return merged
