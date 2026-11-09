"""SQLite persistence layer for the SREL onboarding bot.

Tables:
    roster   - snapshot of the club roster (from Google Sheets)
    claimed  - dedup ledger: which Discord user claimed which callsign
    codes    - one-time email verification codes (MAX_ATTEMPTS wrong tries each)
    settings - small values that outlive a restart (the welcome message's id)
"""
from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS roster (
    callsign   TEXT PRIMARY KEY,
    full_name  TEXT,
    email      TEXT NOT NULL,
    is_officer INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_roster_email ON roster(email);

CREATE TABLE IF NOT EXISTS claimed (
    callsign        TEXT PRIMARY KEY,
    discord_user_id INTEGER NOT NULL,
    email           TEXT NOT NULL,
    claimed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claimed_email ON claimed(email);
CREATE INDEX IF NOT EXISTS idx_claimed_user  ON claimed(discord_user_id);

CREATE TABLE IF NOT EXISTS codes (
    id              INTEGER PRIMARY KEY,
    discord_user_id INTEGER NOT NULL,
    callsign        TEXT NOT NULL,
    email           TEXT NOT NULL,
    code            TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    used_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_codes_user ON codes(discord_user_id, used_at);

CREATE TABLE IF NOT EXISTS review_queue (
    id              INTEGER PRIMARY KEY,
    discord_user_id INTEGER NOT NULL,
    callsign        TEXT NOT NULL,
    email           TEXT NOT NULL,
    reason_code     TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    resolved_at     TEXT,
    resolved_by     INTEGER,
    resolution      TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue(resolved_at);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_user ON review_queue(discord_user_id, resolved_at);
"""


MAX_ATTEMPTS = 5  # wrong guesses before a code is void: 5 in a million, not a million in 15 minutes


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            if "attempts" not in {r[1] for r in c.execute("PRAGMA table_info(codes)")}:  # older bot.db
                c.execute("ALTER TABLE codes ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")

    # --- settings ---

    def get_setting(self, key: str):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return int(row["value"]) if row and row["value"].isdigit() else (row["value"] if row else None)

    def set_setting(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- roster ---

    def replace_roster(self, rows: list[tuple[str, str, str, bool]]) -> None:
        """Wholesale replace. rows = [(callsign, full_name, email, is_officer), ...]."""
        ts = _now()
        with self._conn() as c:
            c.execute("DELETE FROM roster")
            c.executemany(
                "INSERT INTO roster (callsign, full_name, email, is_officer, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(cs, fn, em, 1 if off else 0, ts) for cs, fn, em, off in rows],
            )

    def roster_lookup(self, callsign: str):
        with self._conn() as c:
            return c.execute(
                "SELECT callsign, full_name, email, is_officer FROM roster WHERE callsign = ?",
                (callsign,),
            ).fetchone()

    def roster_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM roster").fetchone()[0]

    def roster_officer_count(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM roster WHERE is_officer = 1"
            ).fetchone()[0]

    def roster_last_sync(self) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(updated_at) AS t FROM roster"
            ).fetchone()
            return row["t"] if row else None

    # --- claimed ---

    def claim_by_callsign(self, callsign: str):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM claimed WHERE callsign = ?", (callsign,)
            ).fetchone()

    def claim_by_email(self, email: str):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM claimed WHERE email = ?", (email.lower(),)
            ).fetchone()

    def add_claim(self, callsign: str, user_id: int, email: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO claimed (callsign, discord_user_id, email, claimed_at) VALUES (?, ?, ?, ?)",
                (callsign, user_id, email.lower(), _now()),
            )

    def release_claim(self, callsign: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM claimed WHERE callsign = ?", (callsign,))

    def all_claims(self):
        """Every claim joined to its roster name, for bulk nickname repair."""
        with self._conn() as c:
            return c.execute(
                "SELECT c.callsign, c.discord_user_id, r.full_name "
                "FROM claimed c LEFT JOIN roster r ON r.callsign = c.callsign "
                "ORDER BY c.callsign"
            ).fetchall()

    def claimed_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM claimed").fetchone()[0]

    # --- review queue ---

    def open_review(
        self, user_id: int, callsign: str, email: str, reason_code: str
    ) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO review_queue (discord_user_id, callsign, email, reason_code, opened_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, callsign, email.lower(), reason_code, _now()),
            )
            return cur.lastrowid

    def get_review(self, review_id: int):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM review_queue WHERE id = ?", (review_id,)
            ).fetchone()

    def resolve_review(
        self, review_id: int, resolved_by: int, resolution: str
    ) -> bool:
        """Returns True if we resolved it, False if it was already resolved."""
        with self._conn() as c:
            cur = c.execute(
                "UPDATE review_queue SET resolved_at = ?, resolved_by = ?, resolution = ? "
                "WHERE id = ? AND resolved_at IS NULL",
                (_now(), resolved_by, resolution, review_id),
            )
            return cur.rowcount > 0

    def open_review_for_user(self, user_id: int):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM review_queue WHERE discord_user_id = ? AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()

    def open_review_count(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL"
            ).fetchone()[0]

    # --- codes ---

    def recent_code_count(self, user_id: int, within_hours: int = 1) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=within_hours)
        ).isoformat()
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM codes WHERE discord_user_id = ? AND created_at >= ?",
                (user_id, cutoff),
            ).fetchone()[0]

    def invalidate_active_codes(self, user_id: int) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE codes SET used_at = ? WHERE discord_user_id = ? AND used_at IS NULL",
                (_now(), user_id),
            )

    def create_code(
        self, user_id: int, callsign: str, email: str, code: str, ttl_min: int
    ) -> None:
        expires = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_min)
        ).isoformat()
        with self._conn() as c:
            c.execute(
                "INSERT INTO codes (discord_user_id, callsign, email, code, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, callsign, email.lower(), code, _now(), expires),
            )

    def consume_code(self, user_id: int, code: str):
        """Atomically mark a code used. Returns (callsign, email) or None. A wrong guess counts against the
        user's active code; after MAX_ATTEMPTS it's void and they ask for a new one."""
        with self._conn() as c:
            row = c.execute(
                """SELECT id, callsign, email, code, expires_at, attempts FROM codes
                   WHERE discord_user_id = ? AND used_at IS NULL
                   ORDER BY id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
            if not row or row["expires_at"] < _now():
                return None
            if not secrets.compare_digest(row["code"], code):
                c.execute("UPDATE codes SET attempts = attempts + 1, used_at = CASE WHEN attempts + 1 >= ? THEN ? END "
                          "WHERE id = ?", (MAX_ATTEMPTS, _now(), row["id"]))
                return None
            c.execute(
                "UPDATE codes SET used_at = ? WHERE id = ?", (_now(), row["id"])
            )
            return row["callsign"], row["email"]
