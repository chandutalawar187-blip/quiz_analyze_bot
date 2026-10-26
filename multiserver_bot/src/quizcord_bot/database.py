"""SQLite persistence for member mappings and Quizcord results."""

import sqlite3
import re
import hashlib
import hmac
import logging
import secrets
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
import time

if TYPE_CHECKING:
    from discord import Member


POINTS_PATTERN = re.compile(r"^\s*(?P<points>\d+)(?:\s+Points?)?\s*$", re.IGNORECASE)
APP_PASSWORD_HASH_ITERATIONS = 310_000
APP_PASSWORD_MAX_ATTEMPTS = 5
APP_PASSWORD_LOCK_SECONDS = 900
DISCORD_EPOCH_MILLISECONDS = 1_420_070_400_000
USER_MENTION_PATTERN = re.compile(r"^<@!?(\d+)>$")
USER_MENTION_IDS_PATTERN = re.compile(r"<@!?(\d+)>")
USN_FORMAT_PATTERN = re.compile(
    r"(?:4GM\d{2}CS\d{3}|U\d{2}E\d{2}CS\d{3})", re.IGNORECASE
)
LOGGER = logging.getLogger("quizcord_analytics.database")


def normalize_usn(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character) != "Cf"
    )


def matches_usn_format(value: str) -> bool:
    return bool(USN_FORMAT_PATTERN.fullmatch(normalize_usn(value)))


def normalize_member_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip("*_`").removeprefix("@").strip()


def _compatible_quiz_ids(left: str | None, right: str | None) -> bool:
    return left is None or right is None or left == right


def _deduplicate_student_quiz_history(
    history: tuple["StudentQuizRecord", ...],
) -> tuple["StudentQuizRecord", ...]:
    unique: list[StudentQuizRecord] = []
    for record in history:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(unique)
                if existing.channel_id == record.channel_id
                and existing.started_at == record.started_at
                and existing.rank == record.rank
                and existing.points == record.points
                and existing.status == record.status
                and _compatible_quiz_ids(
                    existing.quizcord_quiz_id, record.quizcord_quiz_id
                )
            ),
            None,
        )
        if duplicate_index is None:
            unique.append(record)
            continue
        existing = unique[duplicate_index]
        if existing.quizcord_quiz_id is None and record.quizcord_quiz_id is not None:
            unique[duplicate_index] = record
    return tuple(unique)


def _count_distinct_completed_sessions(
    sessions: Iterable[tuple[str, str, str | None]],
) -> int:
    unique: list[tuple[str, str, str | None]] = []
    for session in sessions:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(unique)
                if existing[0] == session[0]
                and existing[1] == session[1]
                and _compatible_quiz_ids(existing[2], session[2])
            ),
            None,
        )
        if duplicate_index is None:
            unique.append(session)
        elif unique[duplicate_index][2] is None and session[2] is not None:
            unique[duplicate_index] = session
    return len(unique)


@dataclass(frozen=True)
class StudentAnalytics:
    discord_id: str
    usn: str
    student_name: str
    quizzes_attended: int
    quizzes_with_points: int
    total_points: int
    average_points_per_scored_quiz: float | None


@dataclass(frozen=True)
class DashboardUser:
    account_name: str
    global_name: str | None
    server_nickname: str | None
    usn: str | None
    student_name: str | None
    name_matches_usn_format: bool


@dataclass(frozen=True)
class QuizSessionAbsentStudent:
    display_name: str
    usn: str | None


@dataclass
class _StudentAggregate:
    discord_id: str
    usn: str
    student_name: str
    quiz_ids: set[str]
    scored_quiz_ids: set[str]
    total_points: int = 0


@dataclass(frozen=True)
class QuizSession:
    session_id: str
    guild_id: int
    channel_id: int
    status: str
    question_count: int
    started_at: str
    completed_at: str | None
    quizcord_quiz_id: str | None
    admin_user_id: int
    admin_dm_channel_id: int | None
    admin_dm_message_id: int | None
    eligible_student_count: int | None = None


@dataclass(frozen=True)
class QuizParticipant:
    usn: str
    rank: int
    points: int
    discord_id: str | None
    eligible: bool = True
    name_matches_usn_format: bool | None = None
    global_name_matches_usn_format: bool | None = None


@dataclass(frozen=True)
class QuizSessionSummary:
    session: QuizSession
    participants: tuple[QuizParticipant, ...]
    total_points: int
    average_points: float
    question_count: int

    @property
    def attendance_count(self) -> int:
        return sum(participant.eligible for participant in self.participants)

    @property
    def attendance_percentage(self) -> float | None:
        eligible_count = self.session.eligible_student_count
        if not eligible_count:
            return None
        return min(100.0, self.attendance_count / eligible_count * 100)


@dataclass(frozen=True)
class StudentQuizRecord:
    session_id: str
    channel_id: int | None
    quizcord_quiz_id: str | None
    status: str
    started_at: str | None
    completed_at: str | None
    rank: int
    points: int


@dataclass(frozen=True)
class StudentQuizAnalytics:
    usn: str
    student_name: str
    quizzes_attended: int
    completed_quizzes: int
    attendance_percentage: float
    total_points: int
    average_points: float | None
    history: tuple[StudentQuizRecord, ...]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _resolve_guild_id(self, guild_id: int | None) -> int:
        if guild_id is not None:
            return guild_id
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT guild_id FROM guild_settings WHERE installed = 1"
            ).fetchall()
        if len(rows) != 1:
            raise ValueError(
                "A server ID is required when multiple servers are configured."
            )
        return int(rows[0][0])

    def initialize(
        self,
        legacy_guild_id: int | None = None,
        default_faculty_role_id: int | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS admin_credentials")
            connection.execute("DROP TABLE IF EXISTS password_reset_tokens")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    status TEXT
                )
                """
            )
            self._migrate_legacy_guild_tables(connection, legacy_guild_id)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS student_registry (
                    guild_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    discord_username TEXT,
                    discord_nickname TEXT,
                    quizcord_username TEXT,
                    usn TEXT,
                    student_name TEXT,
                    PRIMARY KEY (guild_id, discord_id),
                    UNIQUE (guild_id, usn),
                    FOREIGN KEY(discord_id) REFERENCES users(id)
                )
                """
            )
            registry_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(student_registry)")
            }
            for column in (
                "discord_username",
                "discord_nickname",
                "quizcord_username",
            ):
                if column not in registry_columns:
                    connection.execute(
                        f"ALTER TABLE student_registry ADD COLUMN {column} TEXT"
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_student_registry_guild_quizcord_username
                ON student_registry(guild_id, LOWER(TRIM(quizcord_username)))
                WHERE quizcord_username IS NOT NULL
                  AND TRIM(quizcord_username) != ''
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    usn TEXT,
                    quiz_id TEXT,
                    accuracy TEXT,
                    time_taken TEXT,
                    rank TEXT,
                    quiz_date TEXT,
                    UNIQUE(guild_id, usn, quiz_id)
                )
                """
            )
            analytics_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(quiz_analytics)"
                ).fetchall()
            }
            if "quiz_date" not in analytics_columns:
                connection.execute(
                    "ALTER TABLE quiz_analytics ADD COLUMN quiz_date TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id TEXT PRIMARY KEY,
                    guild_name TEXT NOT NULL,
                    faculty_role_id TEXT,
                    installed INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            if legacy_guild_id is not None:
                connection.execute(
                    """
                    INSERT INTO guild_settings (
                        guild_id, guild_name, faculty_role_id, installed
                    )
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        faculty_role_id = COALESCE(
                            guild_settings.faculty_role_id,
                            excluded.faculty_role_id
                        )
                    """,
                    (
                        str(legacy_guild_id),
                        str(legacy_guild_id),
                        (
                            str(default_faculty_role_id)
                            if default_faculty_role_id is not None
                            else None
                        ),
                    ),
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_quizcord_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_sessions (
                    session_id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    question_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT,
                    quizcord_quiz_id TEXT,
                    admin_user_id TEXT NOT NULL,
                    admin_dm_channel_id TEXT,
                    admin_dm_message_id TEXT,
                    eligible_student_count INTEGER
                )
                """
            )
            session_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(quiz_sessions)"
                ).fetchall()
            }
            if "eligible_student_count" not in session_columns:
                connection.execute(
                    "ALTER TABLE quiz_sessions ADD COLUMN eligible_student_count INTEGER"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_member_eligibility (
                    guild_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    display_name TEXT,
                    global_name TEXT,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, discord_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS faculty_student_assignments (
                    guild_id TEXT NOT NULL,
                    faculty_discord_id TEXT NOT NULL,
                    usn TEXT NOT NULL COLLATE NOCASE,
                    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, faculty_discord_id, usn)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS faculty_credentials (
                    guild_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    must_change_password INTEGER NOT NULL DEFAULT 1,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    locked_until REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, discord_id)
                )
                """
            )
            eligibility_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(guild_member_eligibility)"
                ).fetchall()
            }
            if "display_name" not in eligibility_columns:
                connection.execute(
                    "ALTER TABLE guild_member_eligibility ADD COLUMN display_name TEXT"
                )
            if "global_name" not in eligibility_columns:
                connection.execute(
                    "ALTER TABLE guild_member_eligibility ADD COLUMN global_name TEXT"
                )
            if "is_bot" not in eligibility_columns:
                connection.execute(
                    "ALTER TABLE guild_member_eligibility "
                    "ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_quiz_sessions_active_channel
                ON quiz_sessions(channel_id)
                WHERE status = 'active'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_session_updates (
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    revision_key TEXT NOT NULL,
                    question_number INTEGER NOT NULL,
                    PRIMARY KEY (session_id, message_id, revision_key),
                    FOREIGN KEY(session_id) REFERENCES quiz_sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_session_participants (
                    session_id TEXT NOT NULL,
                    usn TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    points INTEGER NOT NULL,
                    discord_id TEXT,
                    eligible INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (session_id, usn),
                    FOREIGN KEY(session_id) REFERENCES quiz_sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_session_nickname_reminders (
                    session_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (session_id, discord_id),
                    FOREIGN KEY(session_id) REFERENCES quiz_sessions(session_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_session_aliases (
                    alias_session_id TEXT PRIMARY KEY,
                    canonical_session_id TEXT NOT NULL
                )
                """
            )
            participant_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(quiz_session_participants)"
                ).fetchall()
            }
            if "eligible" not in participant_columns:
                connection.execute(
                    "ALTER TABLE quiz_session_participants "
                    "ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1"
                )
            self._merge_same_day_quiz_sessions(connection)
            self._reconcile_composite_mention_participants(connection)
            connection.execute(
                """
                DELETE FROM processed_quizcord_messages
                WHERE processed_at < datetime('now', '-30 days')
                """
            )

    @staticmethod
    def _merge_same_day_quiz_sessions(
        connection: sqlite3.Connection,
    ) -> None:
        groups = connection.execute(
            """
            SELECT guild_id, channel_id, substr(started_at, 1, 10) AS quiz_day
            FROM quiz_sessions
            WHERE status != 'merged'
            GROUP BY guild_id, channel_id, quiz_day
            HAVING COUNT(*) > 1
            """
        ).fetchall()
        for guild_id, channel_id, quiz_day in groups:
            sessions = connection.execute(
                """
                SELECT session_id, status, question_count, started_at,
                       completed_at, quizcord_quiz_id, admin_user_id,
                       admin_dm_channel_id, admin_dm_message_id,
                       eligible_student_count,
                       COALESCE(
                           (SELECT MAX(CAST(message_id AS INTEGER))
                            FROM quiz_session_updates
                            WHERE session_id = quiz_sessions.session_id),
                           0
                       ) AS latest_message_id
                FROM quiz_sessions
                WHERE guild_id = ? AND channel_id = ?
                  AND substr(started_at, 1, 10) = ?
                  AND status != 'merged'
                ORDER BY started_at, session_id
                """,
                (guild_id, channel_id, quiz_day),
            ).fetchall()
            quiz_ids = {
                str(row[5]) for row in sessions if row[5] is not None
            }
            if len(quiz_ids) > 1:
                LOGGER.warning(
                    "Keeping separate quiz sessions for guild %s, channel %s "
                    "on %s because they have conflicting Quizcord IDs: %s.",
                    guild_id,
                    channel_id,
                    quiz_day,
                    sorted(quiz_ids),
                )
                continue
            if (
                len({str(row[1]) for row in sessions}) != 1
                or any(row[1] == "completed" for row in sessions)
            ):
                LOGGER.warning(
                    "Keeping separate quiz sessions for guild %s, channel %s "
                    "on %s because their completion states differ or are "
                    "already completed.",
                    guild_id,
                    channel_id,
                    quiz_day,
                )
                continue
            session_ids = [str(row[0]) for row in sessions]
            message_rows = connection.execute(
                """
                SELECT u.session_id, u.message_id
                FROM quiz_session_updates AS u
                JOIN quiz_sessions AS s ON s.session_id = u.session_id
                WHERE s.guild_id = ? AND s.channel_id = ?
                  AND substr(s.started_at, 1, 10) = ?
                  AND s.status != 'merged'
                """,
                (guild_id, channel_id, quiz_day),
            ).fetchall()
            message_ids_by_session = {
                session_id: set() for session_id in session_ids
            }
            for session_id, message_id in message_rows:
                message_ids_by_session[str(session_id)].add(str(message_id))
            session_message_sets = list(message_ids_by_session.values())
            if (
                not session_message_sets[0]
                or any(
                    message_ids != session_message_sets[0]
                    for message_ids in session_message_sets[1:]
                )
            ):
                LOGGER.warning(
                    "Keeping separate quiz sessions for guild %s, channel %s "
                    "on %s because their leaderboard message histories differ.",
                    guild_id,
                    channel_id,
                    quiz_day,
                )
                continue
            analytics_ids = set(session_ids) | quiz_ids
            placeholders = ",".join("?" for _ in analytics_ids)
            existing_analytics = connection.execute(
                f"""
                SELECT 1 FROM quiz_analytics
                WHERE guild_id = ? AND quiz_id IN ({placeholders})
                LIMIT 1
                """,
                (str(guild_id), *sorted(analytics_ids)),
            ).fetchone()
            if existing_analytics:
                LOGGER.warning(
                    "Keeping separate quiz sessions for guild %s, channel %s "
                    "on %s because analytics rows already exist.",
                    guild_id,
                    channel_id,
                    quiz_day,
                )
                continue

            identified_sessions = [
                row for row in sessions if row[5] is not None
            ]
            canonical = (
                identified_sessions[0]
                if identified_sessions
                else sessions[0]
            )
            canonical_id = str(canonical[0])
            duplicate_ids = [
                session_id
                for session_id in session_ids
                if session_id != canonical_id
            ]
            source_sessions = sorted(
                sessions,
                key=lambda row: (int(row[10]), str(row[3]), str(row[0])),
            )

            for source in source_sessions:
                source_id = str(source[0])
                connection.execute(
                    """
                    INSERT OR IGNORE INTO quiz_session_updates (
                        session_id, message_id, revision_key, question_number
                    )
                    SELECT ?, message_id, revision_key, question_number
                    FROM quiz_session_updates
                    WHERE session_id = ?
                    """,
                    (canonical_id, source_id),
                )
                participants = connection.execute(
                    """
                    SELECT usn, rank, points, discord_id, eligible
                    FROM quiz_session_participants
                    WHERE session_id = ?
                    """,
                    (source_id,),
                ).fetchall()
                for participant in participants:
                    connection.execute(
                        """
                        INSERT INTO quiz_session_participants (
                            session_id, usn, rank, points, discord_id, eligible
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(session_id, usn) DO UPDATE SET
                            rank = excluded.rank,
                            points = excluded.points,
                            discord_id = COALESCE(
                                excluded.discord_id,
                                quiz_session_participants.discord_id
                            ),
                            eligible = excluded.eligible
                        """,
                        (canonical_id, *participant),
                    )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO quiz_session_nickname_reminders (
                        session_id, discord_id, sent_at
                    )
                    SELECT ?, discord_id, sent_at
                    FROM quiz_session_nickname_reminders
                    WHERE session_id = ?
                    """,
                    (canonical_id, source_id),
                )

            completed_at_values = [
                str(row[4]) for row in sessions if row[4] is not None
            ]
            status = (
                "completed"
                if any(row[1] == "completed" for row in sessions)
                else (
                    "active"
                    if any(row[1] == "active" for row in sessions)
                    else str(sessions[-1][1])
                )
            )
            latest_session = max(
                sessions,
                key=lambda row: (int(row[10]), str(row[3]), str(row[0])),
            )
            quiz_id = next(iter(quiz_ids), None)
            dashboard_message = next(
                (
                    row
                    for row in sessions
                    if row[7] is not None and row[8] is not None
                ),
                None,
            )
            for source_id in duplicate_ids:
                connection.execute(
                    """
                    INSERT INTO quiz_session_aliases (
                        alias_session_id, canonical_session_id
                    )
                    VALUES (?, ?)
                    ON CONFLICT(alias_session_id) DO UPDATE SET
                        canonical_session_id = excluded.canonical_session_id
                    """,
                    (source_id, canonical_id),
                )
                connection.execute(
                    "UPDATE quiz_sessions SET status = 'merged' WHERE session_id = ?",
                    (source_id,),
                )
            connection.execute(
                """
                UPDATE quiz_sessions
                SET started_at = ?,
                    completed_at = ?,
                    quizcord_quiz_id = ?,
                    status = ?,
                    question_count = (
                        SELECT COUNT(*) FROM quiz_session_updates
                        WHERE session_id = ?
                    ),
                    eligible_student_count = ?,
                    admin_dm_channel_id = ?,
                    admin_dm_message_id = ?
                WHERE session_id = ?
                """,
                (
                    min(str(row[3]) for row in sessions),
                    max(completed_at_values) if completed_at_values else None,
                    quiz_id,
                    status,
                    canonical_id,
                    latest_session[9],
                    dashboard_message[7] if dashboard_message else None,
                    dashboard_message[8] if dashboard_message else None,
                    canonical_id,
                ),
            )

            connection.execute(
                """
                DELETE FROM quiz_session_participants
                WHERE session_id != ? AND session_id IN (
                    SELECT alias_session_id FROM quiz_session_aliases
                    WHERE canonical_session_id = ?
                )
                """,
                (canonical_id, canonical_id),
            )
            connection.execute(
                """
                DELETE FROM quiz_session_updates
                WHERE session_id != ? AND session_id IN (
                    SELECT alias_session_id FROM quiz_session_aliases
                    WHERE canonical_session_id = ?
                )
                """,
                (canonical_id, canonical_id),
            )
            connection.execute(
                """
                DELETE FROM quiz_session_nickname_reminders
                WHERE session_id != ? AND session_id IN (
                    SELECT alias_session_id FROM quiz_session_aliases
                    WHERE canonical_session_id = ?
                )
                """,
                (canonical_id, canonical_id),
            )
            analytics_ids = duplicate_ids + (
                [canonical_id] if quiz_id and quiz_id != canonical_id else []
            )
            if analytics_ids:
                placeholders = ",".join("?" for _ in analytics_ids)
                connection.execute(
                    f"""
                    DELETE FROM quiz_analytics
                    WHERE guild_id = ? AND quiz_id IN ({placeholders})
                    """,
                    (guild_id, *analytics_ids),
                )
            if status == "completed":
                Database._persist_session_results(
                    connection,
                    canonical_id,
                    quiz_id or canonical_id,
                )
            LOGGER.info(
                "Merged %d same-day quiz sessions for guild %s, channel %s "
                "on %s into session %s.",
                len(sessions),
                guild_id,
                channel_id,
                quiz_day,
                canonical_id,
            )

    @staticmethod
    def _migrate_legacy_guild_tables(
        connection: sqlite3.Connection, legacy_guild_id: int | None
    ) -> None:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        session_guilds = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT guild_id FROM quiz_sessions"
            ).fetchall()
        } if "quiz_sessions" in tables else set()
        target_guild_id = legacy_guild_id
        if target_guild_id is None and len(session_guilds) == 1:
            target_guild_id = next(iter(session_guilds))

        registry_columns = (
            {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(student_registry)"
                ).fetchall()
            }
            if "student_registry" in tables
            else set()
        )
        if registry_columns and "guild_id" not in registry_columns:
            row_count = connection.execute(
                "SELECT COUNT(*) FROM student_registry"
            ).fetchone()[0]
            if row_count and target_guild_id is None:
                raise ValueError(
                    "Legacy student mappings need BOT_GUILD_ID to migrate safely."
                )
            columns = (
                "discord_id",
                "discord_username",
                "discord_nickname",
                "quizcord_username",
                "usn",
                "student_name",
            )
            selected = ", ".join(
                name if name in registry_columns else "NULL"
                for name in columns
            )
            connection.execute(
                """
                CREATE TABLE student_registry_multiguild (
                    guild_id TEXT NOT NULL,
                    discord_id TEXT NOT NULL,
                    discord_username TEXT,
                    discord_nickname TEXT,
                    quizcord_username TEXT,
                    usn TEXT,
                    student_name TEXT,
                    PRIMARY KEY (guild_id, discord_id),
                    UNIQUE (guild_id, usn),
                    FOREIGN KEY(discord_id) REFERENCES users(id)
                )
                """
            )
            if row_count:
                connection.execute(
                    f"""
                    INSERT INTO student_registry_multiguild (
                        guild_id, {", ".join(columns)}
                    )
                    SELECT ?, {selected} FROM student_registry
                    """,
                    (str(target_guild_id),),
                )
            connection.execute("DROP TABLE student_registry")
            connection.execute(
                "ALTER TABLE student_registry_multiguild RENAME TO student_registry"
            )

        analytics_columns = (
            {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(quiz_analytics)"
                ).fetchall()
            }
            if "quiz_analytics" in tables
            else set()
        )
        if analytics_columns and "guild_id" not in analytics_columns:
            row_count = connection.execute(
                "SELECT COUNT(*) FROM quiz_analytics"
            ).fetchone()[0]
            if row_count and target_guild_id is None:
                raise ValueError(
                    "Legacy quiz analytics need BOT_GUILD_ID to migrate safely."
                )
            fields = (
                "usn",
                "quiz_id",
                "accuracy",
                "time_taken",
                "rank",
                "quiz_date",
            )
            selected = ", ".join(
                name if name in analytics_columns else "NULL"
                for name in fields
            )
            connection.execute(
                """
                CREATE TABLE quiz_analytics_multiguild (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    usn TEXT,
                    quiz_id TEXT,
                    accuracy TEXT,
                    time_taken TEXT,
                    rank TEXT,
                    quiz_date TEXT,
                    UNIQUE(guild_id, usn, quiz_id)
                )
                """
            )
            if row_count:
                connection.execute(
                    f"""
                    INSERT INTO quiz_analytics_multiguild (
                        guild_id, {", ".join(fields)}
                    )
                    SELECT ?, {selected} FROM quiz_analytics
                    """,
                    (str(target_guild_id),),
                )
            connection.execute("DROP TABLE quiz_analytics")
            connection.execute(
                "ALTER TABLE quiz_analytics_multiguild RENAME TO quiz_analytics"
            )

    def register_guild(
        self, guild_id: int, guild_name: str, faculty_role_id: int | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, guild_name, faculty_role_id, installed
                )
                VALUES (?, ?, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    guild_name = excluded.guild_name,
                    faculty_role_id = COALESCE(
                        excluded.faculty_role_id, guild_settings.faculty_role_id
                    ),
                    installed = 1
                """,
                (
                    str(guild_id),
                    guild_name,
                    str(faculty_role_id) if faculty_role_id else None,
                ),
            )

    def unregister_guild(self, guild_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE guild_settings SET installed = 0 WHERE guild_id = ?",
                (str(guild_id),),
            )

    def get_installed_guilds(self) -> tuple[tuple[int, str], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT guild_id, guild_name FROM guild_settings
                WHERE installed = 1 ORDER BY guild_name COLLATE NOCASE
                """
            ).fetchall()
        return tuple((int(row[0]), str(row[1])) for row in rows)

    def set_faculty_role(
        self, guild_id: int, faculty_role_id: int | None
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE guild_settings SET faculty_role_id = ?
                WHERE guild_id = ? AND installed = 1
                """,
                (
                    str(faculty_role_id) if faculty_role_id else None,
                    str(guild_id),
                ),
            )
            if cursor.rowcount == 0:
                raise LookupError("Server configuration is not registered.")

    def get_faculty_role_id(self, guild_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT faculty_role_id FROM guild_settings WHERE guild_id = ?",
                (str(guild_id),),
            ).fetchone()
        return int(row[0]) if row and row[0] else None

    @staticmethod
    def _reconcile_composite_mention_participants(
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT p.session_id, s.guild_id, s.quizcord_quiz_id, s.status,
                   p.usn, p.rank, p.points
            FROM quiz_session_participants AS p
            JOIN quiz_sessions AS s ON s.session_id = p.session_id
            WHERE p.usn LIKE '%<@%'
            """
        ).fetchall()
        for (
            session_id,
            guild_id,
            quizcord_quiz_id,
            status,
            old_usn,
            rank,
            points,
        ) in rows:
            mention_ids = set(USER_MENTION_IDS_PATTERN.findall(old_usn))
            if not mention_ids:
                continue
            placeholders = ",".join("?" for _ in mention_ids)
            matches = connection.execute(
                f"""
                SELECT discord_id, usn
                FROM student_registry
                WHERE guild_id = ? AND discord_id IN ({placeholders})
                """,
                (guild_id, *mention_ids),
            ).fetchall()
            if len(matches) != 1:
                continue
            discord_id, student_usn = matches[0]
            if student_usn == old_usn:
                continue
            eligibility = connection.execute(
                """
                SELECT is_bot FROM guild_member_eligibility
                WHERE guild_id = ? AND discord_id = ?
                """,
                (guild_id, discord_id),
            ).fetchone()
            is_bot = bool(eligibility[0]) if eligibility else False
            connection.execute(
                """
                INSERT INTO quiz_session_participants (
                    session_id, usn, rank, points, discord_id, eligible
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, usn) DO UPDATE SET
                    rank = MIN(quiz_session_participants.rank, excluded.rank),
                    points = MAX(quiz_session_participants.points, excluded.points),
                    discord_id = excluded.discord_id,
                    eligible = excluded.eligible
                """,
                (
                    session_id,
                    student_usn,
                    rank,
                    points,
                    discord_id,
                    int(not is_bot),
                ),
            )
            connection.execute(
                """
                DELETE FROM quiz_session_participants
                WHERE session_id = ? AND usn = ?
                """,
                (session_id, old_usn),
            )
            if status == "completed":
                quiz_id = quizcord_quiz_id or session_id
                Database._persist_session_results(
                    connection, session_id, quiz_id
                )
                connection.execute(
                    """
                    DELETE FROM quiz_analytics
                    WHERE guild_id = ? AND usn = ? AND quiz_id = ?
                    """,
                    (guild_id, old_usn, quiz_id),
                )

    def start_quiz_session(
        self,
        guild_id: int,
        channel_id: int,
        admin_user_id: int,
        started_at: str,
        eligible_student_count: int | None = None,
    ) -> QuizSession:
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT session_id, started_at,
                       (SELECT MAX(CAST(message_id AS INTEGER))
                        FROM quiz_session_updates
                        WHERE session_id = quiz_sessions.session_id)
                FROM quiz_sessions
                WHERE channel_id = ? AND status = 'active'
                """,
                (str(channel_id),),
            ).fetchone()
            if active:
                try:
                    active_started_at = datetime.fromisoformat(str(active[1]))
                    new_started_at = datetime.fromisoformat(started_at)
                except ValueError:
                    LOGGER.warning(
                        "Keeping active quiz session %s because its start time "
                        "could not be compared with a new start signal.",
                        active[0],
                    )
                    return self._get_session(connection, active[0])
                if active_started_at.tzinfo is None:
                    active_started_at = active_started_at.replace(
                        tzinfo=timezone.utc
                    )
                if new_started_at.tzinfo is None:
                    new_started_at = new_started_at.replace(
                        tzinfo=timezone.utc
                    )
                last_activity_at = active_started_at
                if active[2] is not None:
                    try:
                        message_snowflake = int(active[2])
                    except (TypeError, ValueError):
                        message_snowflake = 0
                    if message_snowflake > 0:
                        activity_timestamp_ms = (
                            (message_snowflake >> 22)
                            + DISCORD_EPOCH_MILLISECONDS
                        )
                        message_activity_at = datetime.fromtimestamp(
                            activity_timestamp_ms / 1000,
                            timezone.utc,
                        )
                        if message_activity_at >= active_started_at:
                            last_activity_at = message_activity_at
                elapsed = new_started_at - last_activity_at
                if elapsed < timedelta(hours=1):
                    return self._get_session(connection, active[0])
                connection.execute(
                    """
                    UPDATE quiz_sessions
                    SET status = 'interrupted'
                    WHERE session_id = ? AND status = 'active'
                    """,
                    (active[0],),
                )
                LOGGER.warning(
                    "Marking stale quiz session %s interrupted before starting "
                    "a new quiz in channel %s.",
                    active[0],
                    channel_id,
                )

            session_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO quiz_sessions (
                    session_id, guild_id, channel_id, status,
                    started_at, admin_user_id, eligible_student_count
                )
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    session_id,
                    str(guild_id),
                    str(channel_id),
                    started_at,
                    str(admin_user_id),
                    eligible_student_count,
                ),
            )
            return self._get_session(connection, session_id)

    def get_active_quiz_session(self, channel_id: int) -> QuizSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE channel_id = ? AND status = 'active'
                """,
                (str(channel_id),),
            ).fetchone()
            return self._get_session(connection, row[0]) if row else None

    def get_recent_quiz_session(self, channel_id: int) -> QuizSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE channel_id = ? AND status != 'merged'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (str(channel_id),),
            ).fetchone()
            return self._get_session(connection, row[0]) if row else None

    def get_latest_completed_quiz_session(
        self, guild_id: int, channel_id: int
    ) -> QuizSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE guild_id = ? AND channel_id = ? AND status = 'completed'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (str(guild_id), str(channel_id)),
            ).fetchone()
            return self._get_session(connection, row[0]) if row else None

    def get_quiz_session_for_message(
        self, channel_id: int, message_id: int
    ) -> QuizSession | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT s.session_id
                FROM quiz_session_updates AS u
                JOIN quiz_sessions AS s ON s.session_id = u.session_id
                WHERE s.channel_id = ? AND u.message_id = ?
                ORDER BY s.started_at DESC
                LIMIT 1
                """,
                (str(channel_id), str(message_id)),
            ).fetchone()
            return self._get_session(connection, row[0]) if row else None

    def get_eligible_member_count(self, guild_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM guild_member_eligibility
                WHERE guild_id = ? AND eligible = 1
                """,
                (str(guild_id),),
            ).fetchone()
        return int(row[0])

    def get_human_member_count(self, guild_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) FROM guild_member_eligibility
                WHERE guild_id = ? AND eligible = 1 AND is_bot = 0
                """,
                (str(guild_id),),
            ).fetchone()
        return int(row[0])

    def get_dashboard_sessions(self) -> tuple[QuizSession, ...]:
        with self._connect() as connection:
            session_ids = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE admin_dm_channel_id IS NOT NULL
                  AND admin_dm_message_id IS NOT NULL
                  AND status != 'merged'
                ORDER BY started_at DESC
                """
            ).fetchall()
            return tuple(
                self._get_session(connection, row[0]) for row in session_ids
            )

    def get_quiz_session_summaries(
        self, guild_id: int, limit: int = 50, offset: int = 0
    ) -> tuple[QuizSessionSummary, ...]:
        with self._connect() as connection:
            session_ids = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE guild_id = ? AND status != 'merged'
                ORDER BY started_at DESC, session_id DESC
                LIMIT ? OFFSET ?
                """,
                (str(guild_id), limit, offset),
            ).fetchall()
            return tuple(
                self._get_quiz_session_summary(connection, row[0])
                for row in session_ids
            )

    def get_student_quiz_analytics(
        self, guild_id: int, usn: str
    ) -> StudentQuizAnalytics:
        guild_id = self._resolve_guild_id(guild_id)
        with self._connect() as connection:
            registry_row = connection.execute(
                """
                SELECT student_name FROM student_registry
                WHERE guild_id = ? AND usn = ? COLLATE NOCASE
                """,
                (str(guild_id), usn),
            ).fetchone()
            completed_sessions = connection.execute(
                """
                SELECT channel_id, started_at, quizcord_quiz_id
                FROM quiz_sessions
                WHERE guild_id = ? AND status = 'completed'
                ORDER BY started_at, session_id
                """,
                (str(guild_id),),
            ).fetchall()
            rows = connection.execute(
                """
                SELECT s.session_id, s.channel_id, s.quizcord_quiz_id, s.status,
                       s.started_at, s.completed_at, p.rank, p.points
                FROM quiz_session_participants AS p
                JOIN quiz_sessions AS s ON s.session_id = p.session_id
                WHERE s.guild_id = ? AND p.usn = ? COLLATE NOCASE
                ORDER BY s.started_at DESC, s.session_id DESC
                """,
                (str(guild_id), usn),
            ).fetchall()
            imported_rows = connection.execute(
                """
                SELECT quiz_id, accuracy, rank, quiz_date
                FROM quiz_analytics
                WHERE guild_id = ? AND usn = ? COLLATE NOCASE
                  AND quiz_id IS NOT NULL
                ORDER BY quiz_id
                """,
                (str(guild_id), usn),
            ).fetchall()

        history = tuple(
            StudentQuizRecord(
                session_id=row[0],
                channel_id=int(row[1]),
                quizcord_quiz_id=row[2],
                status=row[3],
                started_at=row[4],
                completed_at=row[5],
                rank=int(row[6]),
                points=int(row[7]),
            )
            for row in rows
        )
        history = _deduplicate_student_quiz_history(history)
        session_quiz_ids = {
            record.quizcord_quiz_id or record.session_id for record in history
        }
        session_reference_ids = session_quiz_ids | {
            str(row[0]) for row in rows
        }
        imported_history: list[StudentQuizRecord] = []
        imported_quiz_ids: set[str] = set()
        for quiz_id, score_text, rank_text, quiz_date in imported_rows:
            normalized_quiz_id = str(quiz_id)
            imported_quiz_ids.add(normalized_quiz_id)
            if normalized_quiz_id in session_reference_ids:
                continue
            score_match = (
                POINTS_PATTERN.fullmatch(score_text)
                if isinstance(score_text, str)
                else None
            )
            if score_match is None:
                continue
            try:
                rank = int(rank_text)
            except (TypeError, ValueError):
                rank = 0
            imported_history.append(
                StudentQuizRecord(
                    session_id=f"import:{normalized_quiz_id}",
                    channel_id=None,
                    quizcord_quiz_id=normalized_quiz_id,
                    status="historical",
                    started_at=quiz_date,
                    completed_at=None,
                    rank=rank,
                    points=int(score_match.group("points")),
                )
            )
        history = tuple(imported_history) + history
        completed_history = tuple(
            record
            for record in history
            if record.status in {"completed", "historical"}
        )
        completed_session_ids = {
            record.quizcord_quiz_id or record.session_id
            for record in history
            if record.status == "completed"
        }
        legacy_quiz_ids = imported_quiz_ids - session_reference_ids
        completed_quizzes = (
            _count_distinct_completed_sessions(completed_sessions)
            + len(legacy_quiz_ids - completed_session_ids)
        )
        attended_quiz_ids = {
            record.quizcord_quiz_id or record.session_id
            for record in completed_history
        }
        quizzes_attended = len(attended_quiz_ids)
        total_points = sum(record.points for record in completed_history)
        return StudentQuizAnalytics(
            usn=usn,
            student_name=(
                str(registry_row[0])
                if registry_row and registry_row[0]
                else usn
            ),
            quizzes_attended=quizzes_attended,
            completed_quizzes=completed_quizzes,
            attendance_percentage=(
                round(quizzes_attended / completed_quizzes * 100, 1)
                if completed_quizzes
                else 0.0
            ),
            total_points=total_points,
            average_points=(
                total_points / quizzes_attended if quizzes_attended else None
            ),
            history=history,
        )

    def get_quiz_session(self, session_id: str) -> QuizSession | None:
        with self._connect() as connection:
            try:
                return self._get_session(connection, session_id)
            except LookupError:
                return None

    def clear_guild_quiz_history(self, guild_id: int) -> tuple[int, int]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guild_filter = str(guild_id)
            session_count = connection.execute(
                """
                SELECT COUNT(*) FROM quiz_sessions
                WHERE guild_id = ? AND status != 'merged'
                """,
                (guild_filter,),
            ).fetchone()[0]
            analytics_count = connection.execute(
                "SELECT COUNT(*) FROM quiz_analytics WHERE guild_id = ?",
                (guild_filter,),
            ).fetchone()[0]
            session_filter = """
                session_id IN (
                    SELECT session_id FROM quiz_sessions WHERE guild_id = ?
                )
            """
            for table in (
                "quiz_session_updates",
                "quiz_session_participants",
                "quiz_session_nickname_reminders",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE {session_filter}",
                    (guild_filter,),
                )
            connection.execute(
                """
                DELETE FROM quiz_session_aliases
                WHERE canonical_session_id IN (
                    SELECT session_id FROM quiz_sessions WHERE guild_id = ?
                )
                """,
                (guild_filter,),
            )
            connection.execute(
                "DELETE FROM quiz_analytics WHERE guild_id = ?",
                (guild_filter,),
            )
            connection.execute(
                "DELETE FROM quiz_sessions WHERE guild_id = ?",
                (guild_filter,),
            )
        return int(analytics_count), int(session_count)

    @staticmethod
    def _get_session(
        connection: sqlite3.Connection, session_id: str
    ) -> QuizSession:
        session_id = Database._canonical_session_id(connection, session_id)
        row = connection.execute(
            """
            SELECT session_id, guild_id, channel_id, status, question_count,
                   started_at, completed_at, quizcord_quiz_id, admin_user_id,
                   admin_dm_channel_id, admin_dm_message_id, eligible_student_count
            FROM quiz_sessions WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Quiz session {session_id} does not exist.")
        return QuizSession(
            session_id=row[0],
            guild_id=int(row[1]),
            channel_id=int(row[2]),
            status=row[3],
            question_count=int(row[4]),
            started_at=row[5],
            completed_at=row[6],
            quizcord_quiz_id=row[7],
            admin_user_id=int(row[8]),
            admin_dm_channel_id=int(row[9]) if row[9] else None,
            admin_dm_message_id=int(row[10]) if row[10] else None,
            eligible_student_count=int(row[11]) if row[11] is not None else None,
        )

    @staticmethod
    def _canonical_session_id(
        connection: sqlite3.Connection, session_id: str
    ) -> str:
        row = connection.execute(
            """
            SELECT canonical_session_id
            FROM quiz_session_aliases
            WHERE alias_session_id = ?
            """,
            (session_id,),
        ).fetchone()
        return str(row[0]) if row else session_id

    def set_quiz_dashboard_message(
        self, session_id: str, channel_id: int, message_id: int
    ) -> None:
        with self._connect() as connection:
            session_id = self._canonical_session_id(connection, session_id)
            connection.execute(
                """
                UPDATE quiz_sessions
                SET admin_dm_channel_id = ?, admin_dm_message_id = ?
                WHERE session_id = ?
                """,
                (str(channel_id), str(message_id), session_id),
            )

    def record_quiz_leaderboard(
        self,
        session_id: str,
        message_id: int,
        revision_key: str,
        rows: Iterable[tuple[str, str, str]],
        is_final: bool,
        quizcord_quiz_id: str | None = None,
        completed_at: str | None = None,
    ) -> QuizSessionSummary | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session_id = self._canonical_session_id(connection, session_id)
            session = self._get_session(connection, session_id)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO quiz_session_updates (
                    session_id, message_id, revision_key, question_number
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(message_id),
                    revision_key,
                    session.question_count + 1,
                ),
            )
            if cursor.rowcount == 0:
                return None

            question_number = session.question_count + 1
            if is_final:
                connection.execute(
                    "DELETE FROM quiz_session_participants WHERE session_id = ?",
                    (session_id,),
                )
            connection.execute(
                """
                UPDATE quiz_sessions
                SET question_count = ?,
                    status = ?,
                    completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                    quizcord_quiz_id = COALESCE(?, quizcord_quiz_id)
                WHERE session_id = ?
                """,
                (
                    question_number,
                    "completed" if is_final else "active",
                    int(is_final),
                    completed_at,
                    quizcord_quiz_id,
                    session_id,
                ),
            )

            mapped_students = connection.execute(
                """
                SELECT r.discord_id, r.usn, r.student_name,
                       r.discord_username, r.discord_nickname,
                       r.quizcord_username, u.name
                FROM student_registry AS r
                JOIN users AS u ON u.id = r.discord_id
                WHERE r.guild_id = ?
                """,
                (str(session.guild_id),),
            ).fetchall()
            cached_members = connection.execute(
                """
                SELECT e.discord_id,
                       COALESCE(NULLIF(e.display_name, ''), u.name),
                       e.is_bot
                FROM guild_member_eligibility AS e
                LEFT JOIN users AS u ON u.id = e.discord_id
                WHERE e.guild_id = ?
                """,
                (str(session.guild_id),),
            ).fetchall()
            for rank, username, score in rows:
                mention_ids = set(USER_MENTION_IDS_PATTERN.findall(username))
                mention_match = USER_MENTION_PATTERN.fullmatch(username.strip())
                if mention_ids:
                    matches = [
                        item for item in mapped_students
                        if str(item[0]) in mention_ids
                    ]
                else:
                    normalized_name = normalize_member_alias(username)
                    matches = [
                        item
                        for item in mapped_students
                        if normalized_name
                        in {
                            normalize_member_alias(name)
                            for name in item[1:]
                            if isinstance(name, str) and name.strip()
                        }
                    ]

                if len(matches) > 1:
                    continue
                if len(matches) == 1:
                    discord_id = str(matches[0][0])
                    usn = str(matches[0][1])
                    eligibility = connection.execute(
                        """
                        SELECT is_bot FROM guild_member_eligibility
                        WHERE guild_id = ? AND discord_id = ?
                        """,
                        (str(session.guild_id), discord_id),
                    ).fetchone()
                    eligible = not bool(eligibility[0]) if eligibility else True
                elif mention_match and mention_ids:
                    discord_id = next(iter(mention_ids))
                    member_record = connection.execute(
                        """
                        SELECT e.is_bot,
                               COALESCE(NULLIF(e.display_name, ''), u.name)
                        FROM guild_member_eligibility AS e
                        LEFT JOIN users AS u ON u.id = e.discord_id
                        WHERE e.guild_id = ? AND e.discord_id = ?
                        """,
                        (str(session.guild_id), discord_id),
                    ).fetchone()
                    eligible = not bool(member_record[0]) if member_record else False
                    display_name = (
                        str(member_record[1]).strip()
                        if member_record and member_record[1]
                        else "Unmapped Discord member"
                    )
                    usn = display_name
                    connection.execute(
                        """
                        DELETE FROM quiz_session_participants
                        WHERE session_id = ? AND discord_id = ? AND usn != ?
                        """,
                        (session_id, discord_id, usn),
                    )
                elif not mention_ids:
                    normalized_name = normalize_member_alias(username)
                    member_matches = [
                        member
                        for member in cached_members
                        if member[1]
                        and normalize_member_alias(str(member[1])) == normalized_name
                    ]
                    if len(member_matches) == 1:
                        discord_id = str(member_matches[0][0])
                        usn = str(member_matches[0][1]).strip()
                        eligible = not bool(member_matches[0][2])
                        connection.execute(
                            """
                            DELETE FROM quiz_session_participants
                            WHERE session_id = ? AND discord_id = ? AND usn != ?
                            """,
                            (session_id, discord_id, usn),
                        )
                    else:
                        discord_id = None
                        usn = normalized_name.upper()
                        eligible = True
                else:
                    continue
                if not usn:
                    continue
                points = int(score)
                connection.execute(
                    """
                    INSERT INTO quiz_session_participants (
                        session_id, usn, rank, points, discord_id, eligible
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, usn) DO UPDATE SET
                        rank = excluded.rank,
                        points = excluded.points,
                        eligible = excluded.eligible,
                        discord_id = COALESCE(
                            excluded.discord_id,
                            quiz_session_participants.discord_id
                        )
                    """,
                    (session_id, usn, int(rank), points, discord_id, int(eligible)),
                )

            if is_final:
                self._persist_session_results(
                    connection, session_id, quizcord_quiz_id or session_id
                )

            return self._get_quiz_session_summary(connection, session_id)

    def attach_quizcord_id(self, session_id: str, quizcord_quiz_id: str) -> None:
        with self._connect() as connection:
            session_id = self._canonical_session_id(connection, session_id)
            session = self._get_session(connection, session_id)
            old_id = session.quizcord_quiz_id or session.session_id
            connection.execute(
                """
                UPDATE quiz_sessions
                SET quizcord_quiz_id = ?,
                    status = 'completed',
                    completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
                WHERE session_id = ?
                """,
                (quizcord_quiz_id, session_id),
            )
            if old_id != quizcord_quiz_id:
                connection.execute(
                    """
                    INSERT INTO quiz_analytics (
                        guild_id, usn, quiz_id, accuracy, time_taken, rank,
                        quiz_date
                    )
                    SELECT guild_id, usn, ?, accuracy, time_taken, rank,
                           quiz_date
                    FROM quiz_analytics
                    WHERE guild_id = ? AND quiz_id = ?
                    ON CONFLICT(guild_id, usn, quiz_id) DO UPDATE SET
                        accuracy = excluded.accuracy,
                        time_taken = excluded.time_taken,
                        rank = excluded.rank,
                        quiz_date = COALESCE(
                            excluded.quiz_date, quiz_analytics.quiz_date
                        )
                    """,
                    (quizcord_quiz_id, str(session.guild_id), old_id),
                )
                connection.execute(
                    """
                    DELETE FROM quiz_analytics
                    WHERE guild_id = ? AND quiz_id = ?
                    """,
                    (str(session.guild_id), old_id),
                )
            self._persist_session_results(connection, session_id, quizcord_quiz_id)

    @staticmethod
    def _persist_session_results(
        connection: sqlite3.Connection, session_id: str, quiz_id: str
    ) -> None:
        session_id = Database._canonical_session_id(connection, session_id)
        session = Database._get_session(connection, session_id)
        participants = connection.execute(
            """
            SELECT usn, rank, points FROM quiz_session_participants
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchall()
        for usn, rank, points in participants:
            connection.execute(
                """
                INSERT INTO quiz_analytics (
                    guild_id, usn, quiz_id, accuracy, time_taken, rank
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, usn, quiz_id) DO UPDATE SET
                    accuracy = excluded.accuracy,
                    time_taken = excluded.time_taken,
                    rank = excluded.rank
                """,
                (
                    str(session.guild_id),
                    usn,
                    quiz_id,
                    f"{points} Points",
                    "N/A",
                    str(rank),
                ),
            )

    def get_quiz_session_summary(self, session_id: str) -> QuizSessionSummary:
        with self._connect() as connection:
            return self._get_quiz_session_summary(connection, session_id)

    def get_quiz_session_absentees(
        self, session_id: str
    ) -> tuple[QuizSessionAbsentStudent, ...]:
        with self._connect() as connection:
            session = self._get_session(connection, session_id)
            participant_rows = connection.execute(
                """
                SELECT usn, discord_id
                FROM quiz_session_participants
                WHERE session_id = ?
                """,
                (session.session_id,),
            ).fetchall()
            participant_usns = {
                str(row[0]).strip().casefold()
                for row in participant_rows
                if row[0]
            }
            participant_discord_ids = {
                str(row[1]) for row in participant_rows if row[1]
            }
            member_rows = connection.execute(
                """
                SELECT e.discord_id, u.name, e.global_name, e.display_name,
                       r.usn, r.student_name
                FROM guild_member_eligibility AS e
                JOIN users AS u ON u.id = e.discord_id
                LEFT JOIN student_registry AS r
                    ON r.guild_id = e.guild_id AND r.discord_id = e.discord_id
                WHERE e.guild_id = ? AND e.eligible = 1 AND e.is_bot = 0
                ORDER BY
                    COALESCE(
                        NULLIF(r.student_name, ''),
                        NULLIF(e.display_name, ''),
                        NULLIF(e.global_name, ''),
                        u.name
                    ) COLLATE NOCASE
                """,
                (str(session.guild_id),),
            ).fetchall()

        absentees = []
        for discord_id, account_name, global_name, display_name, usn, student_name in member_rows:
            resolved_usn = (
                str(usn).strip()
                if usn
                else (
                    str(global_name).strip()
                    if global_name and matches_usn_format(str(global_name))
                    else None
                )
            )
            member_identities = {
                str(value).strip().casefold()
                for value in (
                    resolved_usn,
                    student_name,
                    global_name,
                    display_name,
                    account_name,
                )
                if value
            }
            if (
                str(discord_id) in participant_discord_ids
                or member_identities.intersection(participant_usns)
            ):
                continue
            absentees.append(
                QuizSessionAbsentStudent(
                    display_name=str(
                        student_name or display_name or global_name or account_name
                    ),
                    usn=resolved_usn,
                )
            )
        return tuple(absentees)

    @classmethod
    def _get_quiz_session_summary(
        cls, connection: sqlite3.Connection, session_id: str
    ) -> QuizSessionSummary:
        session = cls._get_session(connection, session_id)
        session_id = session.session_id
        rows = connection.execute(
            """
            SELECT p.usn, p.rank, p.points, p.discord_id, p.eligible,
                   e.global_name
            FROM quiz_session_participants AS p
            LEFT JOIN guild_member_eligibility AS e
                ON e.guild_id = ? AND e.discord_id = p.discord_id
            WHERE p.session_id = ?
            ORDER BY p.rank, p.usn COLLATE NOCASE
            """,
            (str(session.guild_id), session_id),
        ).fetchall()
        participants = tuple(
            QuizParticipant(
                usn=row[0],
                rank=int(row[1]),
                points=int(row[2]),
                discord_id=row[3],
                eligible=bool(row[4]),
                name_matches_usn_format=(
                    matches_usn_format(str(row[0]))
                    or (
                        row[5] is not None
                        and matches_usn_format(str(row[5]))
                    )
                ),
                global_name_matches_usn_format=(
                    matches_usn_format(str(row[5]))
                    if row[5] is not None
                    else False
                    if row[3] is not None
                    else None
                ),
            )
            for row in rows
        )
        total_points = sum(participant.points for participant in participants)
        return QuizSessionSummary(
            session=session,
            participants=participants,
            total_points=total_points,
            average_points=(
                total_points / len(participants) if participants else 0.0
            ),
            question_count=session.question_count,
        )
    def sync_members(self, members: Iterable["Member"]) -> None:
        with self._connect() as connection:
            for member in members:
                guild = getattr(member, "guild", None)
                guild_id = (
                    int(guild.id)
                    if guild is not None
                    else self._resolve_guild_id(None)
                )
                permissions = getattr(member, "guild_permissions", None)
                if guild is not None and permissions is not None:
                    connection.execute(
                        """
                        INSERT INTO guild_member_eligibility (
                            guild_id, discord_id, eligible, display_name,
                            global_name, is_bot
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, discord_id)
                        DO UPDATE SET
                            eligible = excluded.eligible,
                            display_name = excluded.display_name,
                            global_name = excluded.global_name,
                            is_bot = excluded.is_bot
                        """,
                        (
                            str(guild.id),
                            str(member.id),
                            int(not member.bot),
                            member.display_name,
                            getattr(member, "global_name", None),
                            int(member.bot),
                        ),
                    )
                if member.bot:
                    continue
                connection.execute(
                    """
                    INSERT INTO users (id, name, status)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        status = excluded.status
                    """,
                    (str(member.id), member.name, member.status.name),
                )
                connection.execute(
                    """
                    UPDATE student_registry
                    SET discord_username = ?, discord_nickname = ?
                    WHERE guild_id = ? AND discord_id = ?
                    """,
                    (
                        member.name,
                        member.display_name,
                        str(guild_id),
                        str(member.id),
                    ),
                )

    def sync_member(self, member: "Member") -> None:
        self.sync_members((member,))

    def get_dashboard_users(self, guild_id: int) -> tuple[DashboardUser, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT u.name, e.global_name, e.display_name,
                       r.usn, r.student_name
                FROM guild_member_eligibility AS e
                JOIN users AS u ON u.id = e.discord_id
                LEFT JOIN student_registry AS r
                    ON r.guild_id = e.guild_id AND r.discord_id = e.discord_id
                WHERE e.guild_id = ? AND e.eligible = 1 AND e.is_bot = 0
                ORDER BY
                    COALESCE(NULLIF(e.global_name, ''), u.name) COLLATE NOCASE,
                    u.name COLLATE NOCASE
                """,
                (str(guild_id),),
            ).fetchall()
        return tuple(
            DashboardUser(
                account_name=str(account_name),
                global_name=str(global_name) if global_name else None,
                server_nickname=str(server_nickname) if server_nickname else None,
                usn=(
                    str(usn)
                    if usn
                    else (
                        normalize_usn(str(global_name))
                        if global_name
                        and matches_usn_format(str(global_name))
                        else None
                    )
                ),
                student_name=str(student_name) if student_name else None,
                name_matches_usn_format=matches_usn_format(
                    str(global_name or "")
                ),
            )
            for account_name, global_name, server_nickname, usn, student_name in rows
        )

    def get_faculty_student_usns(
        self, guild_id: int, faculty_discord_id: int
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT usn
                FROM faculty_student_assignments
                WHERE guild_id = ? AND faculty_discord_id = ?
                ORDER BY usn COLLATE NOCASE
                """,
                (str(guild_id), str(faculty_discord_id)),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def add_faculty_student(
        self, guild_id: int, faculty_discord_id: int, usn: str
    ) -> bool:
        normalized_usn = normalize_usn(usn).upper()
        candidate = next(
            (
                user
                for user in self.get_dashboard_users(guild_id)
                if user.usn is not None
                and normalize_usn(user.usn).casefold()
                == normalized_usn.casefold()
            ),
            None,
        )
        if (
            candidate is None
            or not matches_usn_format(candidate.global_name or "")
            or normalized_usn.casefold()
            != normalize_usn(candidate.global_name or "").casefold()
        ):
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO faculty_student_assignments (
                    guild_id, faculty_discord_id, usn
                )
                VALUES (?, ?, ?)
                """,
                (str(guild_id), str(faculty_discord_id), normalized_usn),
            )
        return cursor.rowcount > 0

    def remove_faculty_student(
        self, guild_id: int, faculty_discord_id: int, usn: str
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM faculty_student_assignments
                WHERE guild_id = ? AND faculty_discord_id = ?
                  AND usn = ? COLLATE NOCASE
                """,
                (str(guild_id), str(faculty_discord_id), usn.strip()),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _password_hash(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            APP_PASSWORD_HASH_ITERATIONS,
        ).hex()

    def set_faculty_temporary_password(
        self, guild_id: int, faculty_discord_id: int, password: str
    ) -> None:
        salt = secrets.token_hex(16)
        password_hash = self._password_hash(password, salt)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO faculty_credentials (
                    guild_id, discord_id, password_salt, password_hash,
                    must_change_password, failed_attempts, locked_until,
                    updated_at
                )
                VALUES (?, ?, ?, ?, 1, 0, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                    password_salt = excluded.password_salt,
                    password_hash = excluded.password_hash,
                    must_change_password = 1,
                    failed_attempts = 0,
                    locked_until = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    str(guild_id),
                    str(faculty_discord_id),
                    salt,
                    password_hash,
                ),
            )

    def has_faculty_password(self, guild_id: int, faculty_discord_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM faculty_credentials
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(guild_id), str(faculty_discord_id)),
            ).fetchone()
        return row is not None

    def get_faculty_password_failed_attempts(
        self, guild_id: int, faculty_discord_id: int
    ) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT failed_attempts FROM faculty_credentials
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(guild_id), str(faculty_discord_id)),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def verify_faculty_password(
        self, guild_id: int, faculty_discord_id: int, password: str
    ) -> tuple[bool, bool]:
        now = time.time()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT password_salt, password_hash, must_change_password,
                       failed_attempts, locked_until
                FROM faculty_credentials
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(guild_id), str(faculty_discord_id)),
            ).fetchone()
            if row is None or float(row[4]) > now:
                return False, False
            password_salt, password_hash, must_change, attempts, _ = row
            submitted_hash = self._password_hash(password, password_salt)
            if not hmac.compare_digest(submitted_hash, password_hash):
                attempts = int(attempts) + 1
                lock_until = (
                    now + APP_PASSWORD_LOCK_SECONDS
                    if attempts >= APP_PASSWORD_MAX_ATTEMPTS
                    else 0
                )
                connection.execute(
                    """
                    UPDATE faculty_credentials
                    SET failed_attempts = ?, locked_until = ?
                    WHERE guild_id = ? AND discord_id = ?
                    """,
                    (attempts, lock_until, str(guild_id), str(faculty_discord_id)),
                )
                return False, False
            connection.execute(
                """
                UPDATE faculty_credentials
                SET failed_attempts = 0, locked_until = 0
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(guild_id), str(faculty_discord_id)),
            )
            return True, bool(must_change)

    def change_faculty_password(
        self, guild_id: int, faculty_discord_id: int, password: str
    ) -> None:
        salt = secrets.token_hex(16)
        password_hash = self._password_hash(password, salt)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE faculty_credentials
                SET password_salt = ?, password_hash = ?,
                    must_change_password = 0, failed_attempts = 0,
                    locked_until = 0, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = ? AND discord_id = ?
                """,
                (
                    salt,
                    password_hash,
                    str(guild_id),
                    str(faculty_discord_id),
                ),
            )
            if cursor.rowcount == 0:
                raise LookupError("Faculty password has not been provisioned.")

    def claim_global_name_reminder(
        self, session_id: str, discord_id: int, fallback_usn: str | None = None
    ) -> str | None:
        with self._connect() as connection:
            session_id = self._canonical_session_id(connection, session_id)
            session = self._get_session(connection, session_id)
            row = connection.execute(
                """
                SELECT usn FROM student_registry
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(session.guild_id), str(discord_id)),
            ).fetchone()
            if row is None and fallback_usn is None:
                return None
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO quiz_session_nickname_reminders (
                    session_id, discord_id
                )
                VALUES (?, ?)
                """,
                (session_id, str(discord_id)),
            )
            if not cursor.rowcount:
                return None
            if row is not None:
                return str(row[0])
            return fallback_usn or ""

    def release_global_name_reminder(
        self, session_id: str, discord_id: int
    ) -> None:
        with self._connect() as connection:
            session_id = self._canonical_session_id(connection, session_id)
            connection.execute(
                """
                DELETE FROM quiz_session_nickname_reminders
                WHERE session_id = ? AND discord_id = ?
                """,
                (session_id, str(discord_id)),
            )

    def remove_member(self, member: "Member") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE guild_member_eligibility
                SET eligible = 0
                WHERE guild_id = ? AND discord_id = ?
                """,
                (str(member.guild.id), str(member.id)),
            )

    def map_student(
        self,
        member: "Member",
        usn: str,
        student_name: str,
        quizcord_username: str | None = None,
        guild_id: int | None = None,
    ) -> None:
        guild = getattr(member, "guild", None)
        resolved_guild_id = self._resolve_guild_id(
            guild_id if guild_id is not None else (
                int(guild.id) if guild is not None else None
            )
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (id, name, status)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    status = excluded.status
                """,
                (str(member.id), member.name, member.status.name),
            )
            connection.execute(
                """
                INSERT INTO student_registry (
                    guild_id, discord_id, discord_username, discord_nickname,
                    quizcord_username, usn, student_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, discord_id) DO UPDATE SET
                    discord_username = excluded.discord_username,
                    discord_nickname = excluded.discord_nickname,
                    quizcord_username = COALESCE(
                        excluded.quizcord_username,
                        student_registry.quizcord_username
                    ),
                    usn = excluded.usn,
                    student_name = excluded.student_name
                """,
                (
                    str(resolved_guild_id),
                    str(member.id),
                    member.name,
                    member.display_name,
                    normalize_member_alias(quizcord_username)
                    if quizcord_username and quizcord_username.strip()
                    else None,
                    usn,
                    student_name,
                ),
            )

    def save_quiz_results(
        self,
        quiz_id: str,
        rows: Iterable[tuple[str, str, str]],
        source_message_id: int | None = None,
        include_unmatched: bool = False,
        quiz_date: str | None = None,
        guild_id: int | None = None,
    ) -> list[dict[str, str]] | None:
        guild_id = self._resolve_guild_id(guild_id)
        profiles: dict[str, dict[str, str]] = {}
        with self._connect() as connection:
            if source_message_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM processed_quizcord_messages WHERE message_id = ?",
                    (str(source_message_id),),
                ).fetchone():
                    return None

            guild_filter = str(guild_id)
            tracked_start = connection.execute(
                """
                SELECT started_at
                FROM quiz_sessions
                WHERE quizcord_quiz_id = ?
                  AND (? IS NULL OR guild_id = ?)
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (quiz_id, guild_filter, guild_filter),
            ).fetchone()
            recorded_quiz_date = (
                quiz_date
                or (tracked_start[0] if tracked_start else None)
                or datetime.now(timezone.utc).isoformat()
            )

            mapped_students = connection.execute(
                """
                SELECT r.discord_id, r.usn, r.student_name,
                       r.discord_username, r.discord_nickname,
                       r.quizcord_username, u.name
                FROM student_registry AS r
                JOIN users AS u ON u.id = r.discord_id
                WHERE r.guild_id = ?
                """,
                (guild_filter,),
            ).fetchall()
            for rank, username, score in rows:
                mention_match = USER_MENTION_PATTERN.fullmatch(username.strip())
                if mention_match:
                    matches = {
                        (discord_id, usn, student_name)
                        for discord_id, usn, student_name, *_ in mapped_students
                        if str(discord_id) == mention_match.group(1)
                    }
                else:
                    normalized_name = normalize_member_alias(username)
                    matches = {
                        (discord_id, usn, student_name)
                        for discord_id, usn, student_name, *names in mapped_students
                        if normalized_name
                        in {
                            normalize_member_alias(name)
                            for name in names
                            if isinstance(name, str) and name.strip()
                        }
                    }
                if len(matches) == 1:
                    _, usn, student_name = matches.pop()
                elif include_unmatched:
                    usn = normalize_member_alias(username).upper()
                    student_name = usn
                else:
                    continue
                if not usn:
                    continue
                connection.execute(
                    """
                    INSERT INTO quiz_analytics (
                        guild_id, usn, quiz_id, accuracy, time_taken, rank,
                        quiz_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, usn, quiz_id) DO UPDATE SET
                        accuracy = excluded.accuracy,
                        time_taken = excluded.time_taken,
                        rank = excluded.rank,
                        quiz_date = COALESCE(
                            excluded.quiz_date, quiz_analytics.quiz_date
                        )
                    """,
                    (
                        guild_filter,
                        usn,
                        quiz_id,
                        f"{score} Points",
                        "N/A",
                        rank,
                        recorded_quiz_date,
                    ),
                )
                profiles[usn] = {"usn": usn, "name": student_name}
            if source_message_id is not None:
                connection.execute(
                    "INSERT INTO processed_quizcord_messages (message_id) VALUES (?)",
                    (str(source_message_id),),
                )
        return list(profiles.values())

    def get_quiz_result(
        self, usn: str, quiz_id: str, guild_id: int | None = None
    ) -> tuple[str, str] | None:
        guild_id = self._resolve_guild_id(guild_id)
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT accuracy, rank
                FROM quiz_analytics
                WHERE guild_id = ? AND usn = ? AND quiz_id = ?
                """,
                (str(guild_id), usn, quiz_id),
            ).fetchone()

    def count_attended_quizzes(
        self, usn: str, guild_id: int | None = None
    ) -> int:
        guild_id = self._resolve_guild_id(guild_id)
        with self._connect() as connection:
            result = connection.execute(
                """
                SELECT COUNT(DISTINCT quiz_id)
                FROM quiz_analytics
                WHERE guild_id = ? AND usn = ? AND quiz_id IS NOT NULL
                """,
                (str(guild_id), usn),
            ).fetchone()
        return int(result[0])

    def get_student_analytics(
        self, usn: str, guild_id: int | None = None
    ) -> StudentAnalytics | None:
        report = self.get_analytics_report(guild_id, usn=usn)
        return report[0] if report else None

    def get_analytics_report(
        self, guild_id: int | None = None, usn: str | None = None
    ) -> list[StudentAnalytics]:
        guild_id = self._resolve_guild_id(guild_id)
        query = """
            SELECT r.discord_id, r.usn, r.student_name, qa.quiz_id, qa.accuracy
            FROM student_registry AS r
            JOIN users AS u ON u.id = r.discord_id
            LEFT JOIN quiz_analytics AS qa
                ON qa.guild_id = r.guild_id AND qa.usn = r.usn
            WHERE r.guild_id = ?
        """
        parameters: tuple[str, ...] = (str(guild_id),)
        if usn is not None:
            query += " AND r.usn = ?"
            parameters += (usn,)
        query += " ORDER BY r.student_name COLLATE NOCASE, r.usn, qa.quiz_id"

        aggregates: dict[str, _StudentAggregate] = {}
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()

        for discord_id, student_usn, name, quiz_id, score_text in rows:
            aggregate = aggregates.setdefault(
                student_usn,
                _StudentAggregate(
                    discord_id=discord_id,
                    usn=student_usn,
                    student_name=name,
                    quiz_ids=set(),
                    scored_quiz_ids=set(),
                ),
            )
            if quiz_id is None:
                continue
            aggregate.quiz_ids.add(quiz_id)
            score_match = (
                POINTS_PATTERN.fullmatch(score_text)
                if isinstance(score_text, str)
                else None
            )
            if score_match is None:
                continue
            aggregate.scored_quiz_ids.add(quiz_id)
            aggregate.total_points += int(score_match.group("points"))

        report = []
        for aggregate in aggregates.values():
            quizzes_attended = len(aggregate.quiz_ids)
            quizzes_with_points = len(aggregate.scored_quiz_ids)
            total_points = aggregate.total_points
            report.append(
                StudentAnalytics(
                    discord_id=aggregate.discord_id,
                    usn=aggregate.usn,
                    student_name=aggregate.student_name,
                    quizzes_attended=quizzes_attended,
                    quizzes_with_points=quizzes_with_points,
                    total_points=total_points,
                    average_points_per_scored_quiz=(
                        total_points / quizzes_with_points
                        if quizzes_with_points
                        else None
                    ),
                )
            )
        return report
