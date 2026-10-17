"""SQLite persistence for member mappings and Quizcord results."""

import sqlite3
import re
import unicodedata
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from discord import Member


POINTS_PATTERN = re.compile(r"^\s*(?P<points>\d+)(?:\s+Points?)?\s*$", re.IGNORECASE)
USER_MENTION_PATTERN = re.compile(r"^<@!?(\d+)>$")
USER_MENTION_IDS_PATTERN = re.compile(r"<@!?(\d+)>")
USN_FORMAT_PATTERN = re.compile(
    r"(?:4GM\d{2}CS\d{3}|U\d{2}E\d{2}CS\d{3})", re.IGNORECASE
)


def matches_usn_format(value: str) -> bool:
    return bool(USN_FORMAT_PATTERN.fullmatch(value.strip()))


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

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    status TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS student_registry (
                    discord_id TEXT PRIMARY KEY,
                    discord_username TEXT,
                    discord_nickname TEXT,
                    quizcord_username TEXT,
                    usn TEXT UNIQUE,
                    student_name TEXT,
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
                    idx_student_registry_quizcord_username
                ON student_registry(LOWER(TRIM(quizcord_username)))
                WHERE quizcord_username IS NOT NULL
                  AND TRIM(quizcord_username) != ''
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quiz_analytics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usn TEXT,
                    quiz_id TEXT,
                    accuracy TEXT,
                    time_taken TEXT,
                    rank TEXT,
                    quiz_date TEXT,
                    UNIQUE(usn, quiz_id)
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
            self._reconcile_composite_mention_participants(connection)
            connection.execute(
                """
                DELETE FROM processed_quizcord_messages
                WHERE processed_at < datetime('now', '-30 days')
                """
            )

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
                WHERE discord_id IN ({placeholders})
                """,
                tuple(mention_ids),
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
                    "DELETE FROM quiz_analytics WHERE usn = ? AND quiz_id = ?",
                    (old_usn, quiz_id),
                )

    def start_quiz_session(
        self,
        guild_id: int,
        channel_id: int,
        admin_user_id: int,
        started_at: str,
        force_new: bool = False,
        eligible_student_count: int | None = None,
    ) -> QuizSession:
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT session_id, question_count
                FROM quiz_sessions
                WHERE channel_id = ? AND status = 'active'
                """,
                (str(channel_id),),
            ).fetchone()
            if active and not (force_new and int(active[1]) > 0):
                return self._get_session(connection, active[0])
            if active:
                connection.execute(
                    "UPDATE quiz_sessions SET status = 'interrupted' WHERE session_id = ?",
                    (active[0],),
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
                WHERE channel_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (str(channel_id),),
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
                ORDER BY started_at DESC
                """
            ).fetchall()
            return tuple(
                self._get_session(connection, row[0]) for row in session_ids
            )

    def get_quiz_session_summaries(
        self, guild_id: int, limit: int = 50
    ) -> tuple[QuizSessionSummary, ...]:
        with self._connect() as connection:
            session_ids = connection.execute(
                """
                SELECT session_id FROM quiz_sessions
                WHERE guild_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (str(guild_id), limit),
            ).fetchall()
            return tuple(
                self._get_quiz_session_summary(connection, row[0])
                for row in session_ids
            )

    def get_student_quiz_analytics(
        self, guild_id: int, usn: str
    ) -> StudentQuizAnalytics:
        with self._connect() as connection:
            registry_row = connection.execute(
                """
                SELECT student_name FROM student_registry
                WHERE usn = ? COLLATE NOCASE
                """,
                (usn,),
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
                WHERE usn = ? COLLATE NOCASE AND quiz_id IS NOT NULL
                ORDER BY quiz_id
                """,
                (usn,),
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

    @staticmethod
    def _get_session(
        connection: sqlite3.Connection, session_id: str
    ) -> QuizSession:
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

    def set_quiz_dashboard_message(
        self, session_id: str, channel_id: int, message_id: int
    ) -> None:
        with self._connect() as connection:
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
                """
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
                    "UPDATE quiz_analytics SET quiz_id = ? WHERE quiz_id = ?",
                    (quizcord_quiz_id, old_id),
                )
            self._persist_session_results(connection, session_id, quizcord_quiz_id)

    @staticmethod
    def _persist_session_results(
        connection: sqlite3.Connection, session_id: str, quiz_id: str
    ) -> None:
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
                INSERT INTO quiz_analytics (usn, quiz_id, accuracy, time_taken, rank)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(usn, quiz_id) DO UPDATE SET
                    accuracy = excluded.accuracy,
                    time_taken = excluded.time_taken,
                    rank = excluded.rank
                """,
                (usn, quiz_id, f"{points} Points", "N/A", str(rank)),
            )

    def get_quiz_session_summary(self, session_id: str) -> QuizSessionSummary:
        with self._connect() as connection:
            return self._get_quiz_session_summary(connection, session_id)

    @classmethod
    def _get_quiz_session_summary(
        cls, connection: sqlite3.Connection, session_id: str
    ) -> QuizSessionSummary:
        session = cls._get_session(connection, session_id)
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
                    matches_usn_format(str(row[5]))
                    if row[5] is not None
                    else (
                        False
                        if row[3] is not None
                        else matches_usn_format(str(row[0]))
                    )
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
                    WHERE discord_id = ?
                    """,
                    (member.name, member.display_name, str(member.id)),
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
                LEFT JOIN student_registry AS r ON r.discord_id = e.discord_id
                WHERE e.guild_id = ? AND e.is_bot = 0 AND u.status != 'left'
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
                        str(global_name).strip()
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

    def claim_global_name_reminder(
        self, session_id: str, discord_id: int
    ) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT usn FROM student_registry WHERE discord_id = ?",
                (str(discord_id),),
            ).fetchone()
            if row is None:
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
            return str(row[0]) if cursor.rowcount else None

    def remove_member(self, member: "Member") -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET status = 'left' WHERE id = ?",
                (str(member.id),),
            )
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
    ) -> None:
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
                    discord_id, discord_username, discord_nickname,
                    quizcord_username, usn, student_name
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
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
        profiles: dict[str, dict[str, str]] = {}
        with self._connect() as connection:
            if source_message_id is not None:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(
                    "SELECT 1 FROM processed_quizcord_messages WHERE message_id = ?",
                    (str(source_message_id),),
                ).fetchone():
                    return None

            guild_filter = str(guild_id) if guild_id is not None else None
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
                """
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
                        usn, quiz_id, accuracy, time_taken, rank, quiz_date
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(usn, quiz_id) DO UPDATE SET
                        accuracy = excluded.accuracy,
                        time_taken = excluded.time_taken,
                        rank = excluded.rank,
                        quiz_date = COALESCE(
                            excluded.quiz_date, quiz_analytics.quiz_date
                        )
                    """,
                    (
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
        self, usn: str, quiz_id: str
    ) -> tuple[str, str] | None:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT accuracy, rank
                FROM quiz_analytics
                WHERE usn = ? AND quiz_id = ?
                """,
                (usn, quiz_id),
            ).fetchone()

    def count_attended_quizzes(self, usn: str) -> int:
        with self._connect() as connection:
            result = connection.execute(
                """
                SELECT COUNT(DISTINCT quiz_id)
                FROM quiz_analytics
                WHERE usn = ? AND quiz_id IS NOT NULL
                """,
                (usn,),
            ).fetchone()
        return int(result[0])

    def get_student_analytics(self, usn: str) -> StudentAnalytics | None:
        report = self.get_analytics_report(usn=usn)
        return report[0] if report else None

    def get_analytics_report(
        self, usn: str | None = None
    ) -> list[StudentAnalytics]:
        query = """
            SELECT r.discord_id, r.usn, r.student_name, qa.quiz_id, qa.accuracy
            FROM student_registry AS r
            JOIN users AS u ON u.id = r.discord_id
            LEFT JOIN quiz_analytics AS qa ON qa.usn = r.usn
        """
        parameters: tuple[str, ...] = ()
        if usn is not None:
            query += " WHERE r.usn = ?"
            parameters = (usn,)
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
