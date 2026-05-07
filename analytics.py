"""
analytics.py — Sushi Music Wrapped analytics engine
Stores per-user listening history in PostgreSQL via asyncpg.
Data is never sent anywhere — fully self-hosted.

Required env vars:
    POSTGRES_HOST     (default: localhost)
    POSTGRES_PORT     (default: 5432)
    POSTGRES_DB       (default: sushimusic)
    POSTGRES_USER     (default: sushimusic)
    POSTGRES_PASSWORD (required)
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import asyncpg

log = logging.getLogger("sushimusic.analytics")

# ─── Connection config from env ───────────────────────────────────────────────
_PG_HOST = os.getenv("POSTGRES_HOST", "localhost")
_PG_PORT = int(os.getenv("POSTGRES_PORT", 5432))
_PG_DB   = os.getenv("POSTGRES_DB",   "sushimusic")
_PG_USER = os.getenv("POSTGRES_USER", "sushimusic")
_PG_PASS = os.getenv("POSTGRES_PASSWORD", "")

# ─── Schema (idempotent) ──────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id          BIGSERIAL    PRIMARY KEY,
    guild_id    TEXT         NOT NULL,
    user_id     TEXT         NOT NULL,
    title       TEXT         NOT NULL,
    artist      TEXT         NOT NULL,
    album       TEXT         NOT NULL DEFAULT '',
    duration    INTEGER      NOT NULL DEFAULT 0,
    source_url  TEXT         NOT NULL DEFAULT '',
    played_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plays_guild_user ON plays (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_plays_played_at  ON plays (played_at);
CREATE INDEX IF NOT EXISTS idx_plays_artist     ON plays (artist);
"""


# ─── Dataclasses ─────────────────────────────────────────────────────────────
@dataclass
class WrappedStats:
    user_id: str
    guild_id: str
    year: int

    total_plays: int = 0
    total_minutes: int = 0

    top_tracks: list  = field(default_factory=list)   # [(title, artist, count)]
    top_artists: list = field(default_factory=list)   # [(artist, count, minutes)]

    peak_hour: Optional[int] = None   # 0–23
    peak_day:  Optional[str] = None   # "Monday" … "Sunday"

    first_song:   Optional[dict] = None   # {title, artist, played_at}
    most_recent:  Optional[dict] = None


# ─── AnalyticsDB ─────────────────────────────────────────────────────────────
class AnalyticsDB:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._ready = asyncio.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    async def init(self):
        """Create the connection pool and run migrations. Called once at startup."""
        self._pool = await asyncpg.create_pool(
            host=_PG_HOST,
            port=_PG_PORT,
            database=_PG_DB,
            user=_PG_USER,
            password=_PG_PASS,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
        self._ready.set()
        log.info(f"Analytics DB ready — PostgreSQL {_PG_HOST}:{_PG_PORT}/{_PG_DB}")

    async def close(self):
        if self._pool:
            await self._pool.close()
            log.info("Analytics DB pool closed.")

    # ── Write ──────────────────────────────────────────────────────────────────
    async def record_play(
        self,
        *,
        guild_id: int,
        user_id: int,
        title: str,
        artist: str,
        album: str = "",
        duration: int = 0,
        source_url: str = "",
    ):
        """Insert one play event. Non-blocking — fails silently if DB is down."""
        await self._ready.wait()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO plays
                    (guild_id, user_id, title, artist, album, duration, source_url, played_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                str(guild_id), str(user_id),
                title, artist, album or "Unknown Album",
                duration, source_url or "",
                datetime.now(timezone.utc),
            )
        log.debug(f"Recorded play: [{user_id}] {title} — {artist}")

    # ── Personal Wrapped ───────────────────────────────────────────────────────
    async def get_wrapped(
        self,
        guild_id: int,
        user_id: int,
        year: Optional[int] = None,
    ) -> WrappedStats:
        """Compute per-user Wrapped stats for a given year (default: current year)."""
        await self._ready.wait()
        year = year or datetime.now(timezone.utc).year
        gid, uid = str(guild_id), str(user_id)
        y_start = datetime(year, 1, 1,  tzinfo=timezone.utc)
        y_end   = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

        stats = WrappedStats(user_id=uid, guild_id=gid, year=year)

        async with self._pool.acquire() as conn:

            # Total plays & minutes
            row = await conn.fetchrow(
                """
                SELECT COUNT(*)            AS cnt,
                       COALESCE(SUM(duration), 0) AS secs
                FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                """,
                gid, uid, y_start, y_end,
            )
            stats.total_plays   = row["cnt"] or 0
            stats.total_minutes = int((row["secs"] or 0) // 60)

            # Top 5 tracks
            rows = await conn.fetch(
                """
                SELECT title, artist, COUNT(*) AS play_count
                FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                GROUP BY title, artist
                ORDER BY play_count DESC
                LIMIT 5
                """,
                gid, uid, y_start, y_end,
            )
            stats.top_tracks = [(r["title"], r["artist"], r["play_count"]) for r in rows]

            # Top 5 artists
            rows = await conn.fetch(
                """
                SELECT artist,
                       COUNT(*)                    AS play_count,
                       COALESCE(SUM(duration), 0) AS secs
                FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                GROUP BY artist
                ORDER BY play_count DESC
                LIMIT 5
                """,
                gid, uid, y_start, y_end,
            )
            stats.top_artists = [
                (r["artist"], r["play_count"], int(r["secs"] // 60))
                for r in rows
            ]

            # Peak listening hour (0–23)
            row = await conn.fetchrow(
                """
                SELECT EXTRACT(HOUR FROM played_at AT TIME ZONE 'UTC')::INT AS hr,
                       COUNT(*) AS cnt
                FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                GROUP BY hr
                ORDER BY cnt DESC
                LIMIT 1
                """,
                gid, uid, y_start, y_end,
            )
            stats.peak_hour = row["hr"] if row else None

            # Peak listening day (0=Sunday … 6=Saturday in PostgreSQL DOW)
            row = await conn.fetchrow(
                """
                SELECT EXTRACT(DOW FROM played_at AT TIME ZONE 'UTC')::INT AS dow,
                       COUNT(*) AS cnt
                FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                GROUP BY dow
                ORDER BY cnt DESC
                LIMIT 1
                """,
                gid, uid, y_start, y_end,
            )
            if row:
                _DOW = ["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"]
                stats.peak_day = _DOW[row["dow"]]

            # First song of the year
            row = await conn.fetchrow(
                """
                SELECT title, artist, played_at FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                ORDER BY played_at ASC LIMIT 1
                """,
                gid, uid, y_start, y_end,
            )
            if row:
                stats.first_song = dict(row)

            # Most recent song
            row = await conn.fetchrow(
                """
                SELECT title, artist, played_at FROM plays
                WHERE guild_id=$1 AND user_id=$2
                  AND played_at BETWEEN $3 AND $4
                ORDER BY played_at DESC LIMIT 1
                """,
                gid, uid, y_start, y_end,
            )
            if row:
                stats.most_recent = dict(row)

        return stats

    # ── Server-wide Wrapped ────────────────────────────────────────────────────
    async def get_guild_wrapped(
        self, guild_id: int, year: Optional[int] = None
    ) -> dict:
        """Server-wide Wrapped stats across all users for a given year."""
        await self._ready.wait()
        year = year or datetime.now(timezone.utc).year
        gid    = str(guild_id)
        y_start = datetime(year, 1, 1,  tzinfo=timezone.utc)
        y_end   = datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        result  = {"year": year, "guild_id": gid}

        async with self._pool.acquire() as conn:

            row = await conn.fetchrow(
                """
                SELECT COUNT(*)                    AS cnt,
                       COALESCE(SUM(duration), 0) AS secs
                FROM plays
                WHERE guild_id=$1 AND played_at BETWEEN $2 AND $3
                """,
                gid, y_start, y_end,
            )
            result["total_plays"]   = row["cnt"] or 0
            result["total_minutes"] = int((row["secs"] or 0) // 60)

            rows = await conn.fetch(
                """
                SELECT title, artist, COUNT(*) AS cnt
                FROM plays
                WHERE guild_id=$1 AND played_at BETWEEN $2 AND $3
                GROUP BY title, artist ORDER BY cnt DESC LIMIT 5
                """,
                gid, y_start, y_end,
            )
            result["top_tracks"] = [(r["title"], r["artist"], r["cnt"]) for r in rows]

            rows = await conn.fetch(
                """
                SELECT artist, COUNT(*) AS cnt
                FROM plays
                WHERE guild_id=$1 AND played_at BETWEEN $2 AND $3
                GROUP BY artist ORDER BY cnt DESC LIMIT 5
                """,
                gid, y_start, y_end,
            )
            result["top_artists"] = [(r["artist"], r["cnt"]) for r in rows]

            rows = await conn.fetch(
                """
                SELECT user_id, COUNT(*) AS cnt
                FROM plays
                WHERE guild_id=$1 AND played_at BETWEEN $2 AND $3
                GROUP BY user_id ORDER BY cnt DESC LIMIT 5
                """,
                gid, y_start, y_end,
            )
            result["top_listeners"] = [(r["user_id"], r["cnt"]) for r in rows]

        return result


# ─── Singleton ────────────────────────────────────────────────────────────────
db = AnalyticsDB()