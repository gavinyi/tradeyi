from __future__ import annotations

from io import StringIO
from pathlib import Path
import sqlite3
import time

import pandas as pd


DB_FILE = Path("data/option-chain-cache.db")


def _connection() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_FILE, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS frames (
        cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS api_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT NOT NULL, requested_at REAL NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT, requested_at REAL NOT NULL
        )"""
    )
    return connection


def get_frame(
    cache_key: str, ttl_seconds: int, allow_stale: bool = False,
    max_stale_seconds: int | None = None,
) -> pd.DataFrame | None:
    with _connection() as connection:
        row = connection.execute(
            "SELECT payload,updated_at FROM frames WHERE cache_key=?", (cache_key,)
        ).fetchone()
    age = float("inf") if row is None else time.time() - float(row[1])
    if row is None or (not allow_stale and age > ttl_seconds):
        return None
    if max_stale_seconds is not None and age > max_stale_seconds:
        return None
    return pd.read_json(StringIO(row[0]), orient="split")


def save_frame(cache_key: str, frame: pd.DataFrame) -> None:
    payload = frame.to_json(orient="split", date_format="iso")
    with _connection() as connection:
        connection.execute(
            """INSERT INTO frames(cache_key,payload,updated_at) VALUES(?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
            payload=excluded.payload,updated_at=excluded.updated_at""",
            (cache_key, payload, time.time()),
        )


def acquire_option_chain_slot(max_calls: int = 9, window_seconds: int = 30) -> float:
    """Reserve a cross-process API slot, leaving one Futu call as safety margin."""
    while True:
        now = time.time()
        with _connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM calls WHERE requested_at < ?", (now - window_seconds,)
            )
            timestamps = [
                float(row[0]) for row in connection.execute(
                    "SELECT requested_at FROM calls ORDER BY requested_at"
                ).fetchall()
            ]
            if len(timestamps) < max_calls:
                connection.execute(
                    "INSERT INTO calls(requested_at) VALUES(?)", (now,)
                )
                connection.commit()
                return 0.0
            wait_seconds = max(0.1, window_seconds - (now - timestamps[0]) + 0.1)
        time.sleep(wait_seconds)


def acquire_api_slot(bucket: str, max_calls: int, window_seconds: int = 30) -> float:
    """Reserve a cross-process API slot for a Futu endpoint family."""
    while True:
        now = time.time()
        with _connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM api_calls WHERE bucket=? AND requested_at < ?",
                (bucket, now - window_seconds),
            )
            timestamps = [float(row[0]) for row in connection.execute(
                "SELECT requested_at FROM api_calls WHERE bucket=? ORDER BY requested_at",
                (bucket,),
            ).fetchall()]
            if len(timestamps) < max_calls:
                connection.execute(
                    "INSERT INTO api_calls(bucket,requested_at) VALUES(?,?)", (bucket, now)
                )
                connection.commit()
                return 0.0
            wait_seconds = max(0.1, window_seconds - (now - timestamps[0]) + 0.1)
        time.sleep(wait_seconds)
