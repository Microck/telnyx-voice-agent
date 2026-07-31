"""Persistent call results and transcripts backed by SQLite."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CallStore:
    """Own all persistent state for outbound calls."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_control_id TEXT PRIMARY KEY,
                    to_number TEXT NOT NULL,
                    task TEXT NOT NULL,
                    language TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome_status TEXT,
                    outcome TEXT,
                    cost REAL,
                    currency TEXT,
                    billed_seconds INTEGER,
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS transcript_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_control_id TEXT NOT NULL REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_recordings (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    file_path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_limits (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    max_duration_seconds INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_conversations (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    conversation_id TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_openings (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    opening_line TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operator_requests (
                    token_hash TEXT PRIMARY KEY,
                    call_control_id TEXT NOT NULL REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    question TEXT NOT NULL,
                    proposed_action TEXT,
                    status TEXT NOT NULL,
                    response TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS outcome_notifications (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    claimed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT,
                    raw_body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    processed_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS call_requests (
                    request_id TEXT PRIMARY KEY,
                    call_control_id TEXT UNIQUE,
                    to_number TEXT NOT NULL,
                    task TEXT NOT NULL,
                    language TEXT NOT NULL,
                    max_duration_seconds INTEGER NOT NULL,
                    opening_line TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS operator_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL REFERENCES operator_requests(token_hash) ON DELETE CASCADE,
                    call_control_id TEXT NOT NULL REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    delivered_at TEXT,
                    last_error TEXT,
                    UNIQUE(token_hash, message)
                );

                CREATE TABLE IF NOT EXISTS provider_recording_deletions (
                    recording_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    deleted_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS hangup_requests (
                    call_control_id TEXT PRIMARY KEY REFERENCES calls(call_control_id) ON DELETE CASCADE,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS webhook_events_pending_idx
                    ON webhook_events(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS operator_deliveries_pending_idx
                    ON operator_deliveries(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS calls_created_at_idx
                    ON calls(created_at);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(operator_requests)")
            }
            if "sensitive_field" not in columns:
                connection.execute(
                    "ALTER TABLE operator_requests ADD COLUMN sensitive_field TEXT"
                )
            if "sensitive_reason" not in columns:
                connection.execute(
                    "ALTER TABLE operator_requests ADD COLUMN sensitive_reason TEXT"
                )
            call_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(calls)")
            }
            if "outcome_source" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN outcome_source TEXT"
                )
            if "transcript_final" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN transcript_final INTEGER NOT NULL DEFAULT 0"
                )
            if "transcript_attempts" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN transcript_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "transcript_next_attempt_at" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN transcript_next_attempt_at TEXT"
                )
            if "transcript_candidate_hash" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN transcript_candidate_hash TEXT"
                )
            if "transcript_candidate_at" not in call_columns:
                connection.execute(
                    "ALTER TABLE calls ADD COLUMN transcript_candidate_at TEXT"
                )
            connection.execute(
                """
                UPDATE calls
                SET transcript_final = 0
                WHERE transcript_final = 1
                  AND NOT EXISTS (
                      SELECT 1 FROM transcript_entries
                      WHERE transcript_entries.call_control_id = calls.call_control_id
                  )
                """
            )
            notification_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(outcome_notifications)")
            }
            if "delivered_at" not in notification_columns:
                connection.execute(
                    "ALTER TABLE outcome_notifications ADD COLUMN delivered_at TEXT"
                )
                # Legacy rows were claims created only after an attempted send.
                # Mark them delivered rather than replaying old notifications.
                connection.execute(
                    "UPDATE outcome_notifications SET delivered_at = claimed_at"
                )
            if "attempts" not in notification_columns:
                connection.execute(
                    "ALTER TABLE outcome_notifications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_at" not in notification_columns:
                connection.execute(
                    "ALTER TABLE outcome_notifications ADD COLUMN next_attempt_at TEXT"
                )
                connection.execute(
                    "UPDATE outcome_notifications SET next_attempt_at = claimed_at WHERE next_attempt_at IS NULL"
                )
            if "last_error" not in notification_columns:
                connection.execute(
                    "ALTER TABLE outcome_notifications ADD COLUMN last_error TEXT"
                )
            recording_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(call_recordings)")
            }
            if "recording_id" not in recording_columns:
                connection.execute(
                    "ALTER TABLE call_recordings ADD COLUMN recording_id TEXT"
                )

    def is_healthy(self) -> bool:
        with self._connect() as connection:
            return connection.execute("SELECT 1").fetchone()[0] == 1

    def create_call(
        self,
        call_control_id: str,
        to_number: str,
        task: str,
        language: str,
        max_duration_seconds: int = 300,
        created_at: str | None = None,
        opening_line: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO calls (
                    call_control_id, to_number, task, language, status, created_at
                ) VALUES (?, ?, ?, ?, 'initiated', ?)
                """,
                (call_control_id, to_number, task, language, created_at or _now()),
            )
            connection.execute(
                """
                INSERT INTO call_limits (call_control_id, max_duration_seconds)
                VALUES (?, ?)
                """,
                (call_control_id, max_duration_seconds),
            )
            if opening_line:
                connection.execute(
                    """
                    INSERT INTO call_openings (call_control_id, opening_line)
                    VALUES (?, ?)
                    """,
                    (call_control_id, opening_line.strip()),
                )

    def reserve_call_request(
        self,
        request_id: str,
        to_number: str,
        task: str,
        language: str,
        max_duration_seconds: int,
        opening_line: str | None,
    ) -> dict:
        """Persist call intent before any paid external action."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO call_requests (
                    request_id, to_number, task, language,
                    max_duration_seconds, opening_line, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    request_id,
                    to_number,
                    task,
                    language,
                    max_duration_seconds,
                    opening_line,
                    _now(),
                ),
            )
            request = connection.execute(
                "SELECT * FROM call_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        expected = (
            to_number,
            task,
            language,
            max_duration_seconds,
            opening_line,
        )
        actual = (
            request["to_number"],
            request["task"],
            request["language"],
            request["max_duration_seconds"],
            request["opening_line"],
        )
        if actual != expected:
            raise ValueError("Idempotency-Key was already used for another call request.")
        return dict(request)

    def call_start_blocker(
        self,
        max_concurrent: int = 1,
        max_per_hour: int = 10,
    ) -> str | None:
        """Apply a small local blast-radius limit to the paid endpoint."""
        hour_ago = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        active_cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT COUNT(*) FROM calls
                WHERE status NOT IN ('completed', 'failed') AND created_at >= ?
                """,
                (active_cutoff,),
            ).fetchone()[0]
            recent = connection.execute(
                """
                SELECT COUNT(*) FROM call_requests
                WHERE created_at >= ? AND status != 'failed'
                """,
                (hour_ago,),
            ).fetchone()[0]
        if active >= max_concurrent:
            return "Another call is already active."
        if recent >= max_per_hour:
            return "The local limit of 10 calls per hour has been reached."
        return None

    def materialize_call_request(
        self,
        request_id: str,
        call_control_id: str,
    ) -> bool:
        """Attach Telnyx's call ID and create the local call atomically."""
        with self._connect() as connection:
            request = connection.execute(
                "SELECT * FROM call_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if request is None:
                return False
            connection.execute(
                """
                INSERT OR IGNORE INTO calls (
                    call_control_id, to_number, task, language, status, created_at
                ) VALUES (?, ?, ?, ?, 'initiated', ?)
                """,
                (
                    call_control_id,
                    request["to_number"],
                    request["task"],
                    request["language"],
                    request["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO call_limits (
                    call_control_id, max_duration_seconds
                ) VALUES (?, ?)
                """,
                (call_control_id, request["max_duration_seconds"]),
            )
            if request["opening_line"]:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO call_openings (
                        call_control_id, opening_line
                    ) VALUES (?, ?)
                    """,
                    (call_control_id, request["opening_line"]),
                )
            connection.execute(
                """
                UPDATE call_requests
                SET call_control_id = ?, status = 'dialed', last_error = NULL
                WHERE request_id = ?
                """,
                (call_control_id, request_id),
            )
        return True

    def fail_call_request(self, request_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE call_requests SET status = 'failed', last_error = ?
                WHERE request_id = ?
                """,
                (error[:1000], request_id),
            )

    def set_status(self, call_control_id: str, status: str) -> bool:
        timestamp_column = {
            "answered": "answered_at",
            "completed": "finished_at",
            "failed": "finished_at",
        }.get(status)
        with self._connect() as connection:
            if status == "answered":
                cursor = connection.execute(
                    """
                    UPDATE calls
                    SET status = 'answered', answered_at = COALESCE(answered_at, ?)
                    WHERE call_control_id = ?
                      AND status NOT IN ('answered', 'completed', 'failed')
                    """,
                    (_now(), call_control_id),
                )
            elif timestamp_column:
                cursor = connection.execute(
                    f"""
                    UPDATE calls
                    SET status = ?, {timestamp_column} = COALESCE({timestamp_column}, ?)
                    WHERE call_control_id = ?
                      AND status NOT IN ('completed', 'failed')
                    """,
                    (status, _now(), call_control_id),
                )
            else:
                allowed_from = {
                    "initiated": ("initiated",),
                    "ringing": ("initiated", "ringing"),
                }.get(status, ("initiated", "ringing", "answered"))
                placeholders = ", ".join("?" for _ in allowed_from)
                cursor = connection.execute(
                    f"""
                    UPDATE calls SET status = ?
                    WHERE call_control_id = ?
                      AND status IN ({placeholders})
                    """,
                    (status, call_control_id, *allowed_from),
                )
        return cursor.rowcount == 1

    def append_transcript(self, call_control_id: str, speaker: str, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcript_entries (call_control_id, speaker, text, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (call_control_id, speaker, cleaned, _now()),
            )

    def replace_transcript(
        self,
        call_control_id: str,
        entries: list[tuple[str, str]],
        final: bool = False,
    ) -> bool:
        """Replace provisional messages with Telnyx's final ordered transcript."""
        if final and not entries:
            return False
        with self._connect() as connection:
            call = connection.execute(
                "SELECT transcript_final FROM calls WHERE call_control_id = ?",
                (call_control_id,),
            ).fetchone()
            if call is None or (call["transcript_final"] and not final):
                return False
            connection.execute(
                "DELETE FROM transcript_entries WHERE call_control_id = ?",
                (call_control_id,),
            )
            connection.executemany(
                """
                INSERT INTO transcript_entries (call_control_id, speaker, text, created_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (call_control_id, speaker, text.strip(), _now())
                    for speaker, text in entries
                    if text.strip()
                ],
            )
            if final:
                connection.execute(
                    """
                    UPDATE calls
                    SET transcript_final = 1, transcript_next_attempt_at = NULL
                    WHERE call_control_id = ?
                    """,
                    (call_control_id,),
                )
        return True

    def defer_final_transcript(self, call_control_id: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(seconds=15)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE calls
                SET transcript_attempts = transcript_attempts + 1,
                    transcript_next_attempt_at = ?
                WHERE call_control_id = ? AND transcript_final = 0
                """,
                (retry_at, call_control_id),
            )

    def stage_final_transcript(
        self,
        call_control_id: str,
        entries: list[tuple[str, str]],
        settle_seconds: int = 10,
    ) -> bool:
        """Finalize only after Telnyx returns the same non-empty snapshot twice."""
        if not entries:
            self.defer_final_transcript(call_control_id)
            return False
        snapshot = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        snapshot_hash = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            call = connection.execute(
                """
                SELECT transcript_candidate_hash, transcript_candidate_at
                FROM calls WHERE call_control_id = ?
                """,
                (call_control_id,),
            ).fetchone()
        now = datetime.now(UTC)
        stable = (
            call is not None
            and call["transcript_candidate_hash"] == snapshot_hash
            and call["transcript_candidate_at"] is not None
            and now - datetime.fromisoformat(call["transcript_candidate_at"])
            >= timedelta(seconds=settle_seconds)
        )
        if stable:
            return self.replace_transcript(call_control_id, entries, final=True)
        self.replace_transcript(call_control_id, entries, final=False)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE calls
                SET transcript_candidate_hash = ?, transcript_candidate_at = ?
                WHERE call_control_id = ? AND transcript_final = 0
                """,
                (snapshot_hash, now.isoformat(), call_control_id),
            )
        self.defer_final_transcript(call_control_id)
        return False

    def calls_needing_final_transcript(self, limit: int = 20) -> list[dict]:
        """Find completed conversations whose final transcript is not stored."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT calls.call_control_id, conversations.conversation_id
                FROM calls
                JOIN call_conversations AS conversations
                  ON conversations.call_control_id = calls.call_control_id
                WHERE calls.status = 'completed'
                  AND calls.transcript_final = 0
                  AND (
                      calls.transcript_next_attempt_at IS NULL
                      OR calls.transcript_next_attempt_at <= ?
                  )
                ORDER BY calls.finished_at
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_conversation_id(self, call_control_id: str, conversation_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO call_conversations (call_control_id, conversation_id)
                VALUES (?, ?)
                ON CONFLICT(call_control_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id
                """,
                (call_control_id, conversation_id),
            )
            connection.execute(
                """
                UPDATE calls
                SET transcript_next_attempt_at = COALESCE(transcript_next_attempt_at, ?)
                WHERE call_control_id = ?
                """,
                (_now(), call_control_id),
            )

    def set_outcome(
        self,
        call_control_id: str,
        outcome_status: str,
        outcome: str,
        source: str = "assistant",
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE calls
                SET outcome_status = ?, outcome = ?, outcome_source = ?
                WHERE call_control_id = ?
                  AND (outcome_status IS NULL OR outcome_source = 'fallback')
                """,
                (outcome_status, outcome.strip(), source, call_control_id),
            )
        return cursor.rowcount == 1

    def set_outcome_and_queue_hangup(
        self,
        call_control_id: str,
        outcome_status: str,
        outcome: str,
    ) -> bool:
        """Persist a validated assistant result and its hangup atomically."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE calls
                SET outcome_status = ?, outcome = ?, outcome_source = 'assistant'
                WHERE call_control_id = ?
                  AND (outcome_status IS NULL OR outcome_source = 'fallback')
                """,
                (outcome_status, outcome.strip(), call_control_id),
            )
            if cursor.rowcount == 1:
                hangup_at = (datetime.now(UTC) + timedelta(seconds=6)).isoformat()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO hangup_requests (
                        call_control_id, next_attempt_at
                    ) VALUES (?, ?)
                    """,
                    (call_control_id, hangup_at),
                )
        return cursor.rowcount == 1

    def pending_hangups(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM hangup_requests
                WHERE completed_at IS NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_hangup(self, call_control_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE hangup_requests SET completed_at = ?, last_error = NULL
                WHERE call_control_id = ?
                """,
                (_now(), call_control_id),
            )

    def fail_hangup(self, call_control_id: str, error: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(seconds=3)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE hangup_requests
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE call_control_id = ?
                """,
                (retry_at, error[:1000], call_control_id),
            )

    def create_operator_request(
        self,
        token_hash: str,
        call_control_id: str,
        kind: str,
        question: str,
        proposed_action: str | None,
        expires_at: str,
        sensitive_field: str | None = None,
        sensitive_reason: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operator_requests (
                    token_hash, call_control_id, kind, question,
                    proposed_action, status, created_at, expires_at,
                    sensitive_field, sensitive_reason
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    call_control_id,
                    kind,
                    question.strip(),
                    proposed_action.strip() if proposed_action else None,
                    _now(),
                    expires_at,
                    sensitive_field.lower() if sensitive_field else None,
                    sensitive_reason.strip() if sensitive_reason else None,
                ),
            )

    def get_operator_request(self, token_hash: str) -> dict | None:
        with self._connect() as connection:
            request = connection.execute(
                "SELECT * FROM operator_requests WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return dict(request) if request else None

    def complete_operator_request(
        self,
        token_hash: str,
        status: str,
        response: str,
        delivery_message: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE operator_requests
                SET status = ?, response = ?, completed_at = ?
                WHERE token_hash = ? AND status = 'pending'
                """,
                (status, response.strip(), _now(), token_hash),
            )
            if cursor.rowcount == 1 and delivery_message:
                request = connection.execute(
                    """
                    SELECT call_control_id FROM operator_requests
                    WHERE token_hash = ?
                    """,
                    (token_hash,),
                ).fetchone()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO operator_deliveries (
                        token_hash, call_control_id, message, next_attempt_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        request["call_control_id"],
                        delivery_message,
                        _now(),
                    ),
                )
        return cursor.rowcount == 1

    def consume_sensitive_approval(
        self,
        call_control_id: str,
        field: str,
    ) -> bool:
        """Atomically consume one explicit approval for a sensitive field."""
        with self._connect() as connection:
            request = connection.execute(
                """
                SELECT token_hash
                FROM operator_requests
                WHERE call_control_id = ?
                  AND kind = 'information'
                  AND status = 'approved'
                  AND sensitive_field = ?
                  AND expires_at > ?
                ORDER BY completed_at DESC
                LIMIT 1
                """,
                (call_control_id, field.lower(), _now()),
            ).fetchone()
            if request is None:
                return False
            cursor = connection.execute(
                """
                UPDATE operator_requests
                SET status = 'consumed'
                WHERE token_hash = ? AND status = 'approved'
                """,
                (request["token_hash"],),
            )
        return cursor.rowcount == 1

    def enqueue_webhook_event(
        self,
        event_id: str,
        occurred_at: str | None,
        raw_body: str,
    ) -> bool:
        """Persist a Telnyx event once before acknowledging its webhook."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO webhook_events (
                    event_id, occurred_at, raw_body, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, occurred_at, raw_body, _now(), _now()),
            )
        return cursor.rowcount == 1

    def pending_webhook_events(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM webhook_events
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY created_at
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_webhook_event(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE webhook_events
                SET status = 'processed', processed_at = ?, last_error = NULL
                WHERE event_id = ?
                """,
                (_now(), event_id),
            )

    def fail_webhook_event(self, event_id: str, error: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE webhook_events
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE event_id = ?
                """,
                (retry_at, error[:1000], event_id),
            )

    def pending_operator_deliveries(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operator_deliveries
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY id
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_operator_delivery(self, delivery_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operator_deliveries
                SET status = 'delivered', delivered_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (_now(), delivery_id),
            )

    def fail_operator_delivery(self, delivery_id: int, error: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE operator_deliveries
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE id = ?
                """,
                (retry_at, error[:1000], delivery_id),
            )

    def expire_operator_requests(self) -> int:
        """Close expired requests and queue their assistant notification."""
        message = (
            "[OPERATOR RESPONSE] Marcos did not respond. Do not approve or "
            "invent the requested information. Tell the recipient that Marcos "
            "could not confirm it, then finish the call politely."
        )
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT token_hash, call_control_id FROM operator_requests
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (_now(),),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE operator_requests
                    SET status = 'expired', response = ?, completed_at = ?
                    WHERE token_hash = ? AND status = 'pending'
                    """,
                    ("Marcos did not respond before the approval window closed.", _now(), row["token_hash"]),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO operator_deliveries (
                        token_hash, call_control_id, message, next_attempt_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (row["token_hash"], row["call_control_id"], message, _now()),
                )
        return len(rows)

    def claim_outcome_notification(self, call_control_id: str) -> bool:
        """Claim one end-of-call notification so duplicate webhooks stay quiet."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO outcome_notifications (
                    call_control_id, claimed_at, next_attempt_at
                ) VALUES (?, ?, ?)
                """,
                (call_control_id, _now(), _now()),
            )
        return cursor.rowcount == 1

    def release_outcome_notification(self, call_control_id: str) -> None:
        """Allow a later webhook or manual retry after delivery failed."""
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM outcome_notifications WHERE call_control_id = ?",
                (call_control_id,),
            )

    def pending_outcome_notifications(self, limit: int = 20) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT outcome_notifications.call_control_id
                FROM outcome_notifications
                JOIN calls
                  ON calls.call_control_id = outcome_notifications.call_control_id
                WHERE outcome_notifications.delivered_at IS NULL
                  AND outcome_notifications.next_attempt_at <= ?
                  AND calls.status IN ('completed', 'failed')
                  AND calls.transcript_final = 1
                ORDER BY outcome_notifications.claimed_at
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [row["call_control_id"] for row in rows]

    def complete_outcome_notification(self, call_control_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outcome_notifications
                SET delivered_at = ?, last_error = NULL
                WHERE call_control_id = ?
                """,
                (_now(), call_control_id),
            )

    def fail_outcome_notification(self, call_control_id: str, error: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE outcome_notifications
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE call_control_id = ?
                """,
                (retry_at, error[:1000], call_control_id),
            )

    def set_cost(
        self,
        call_control_id: str,
        cost: float,
        currency: str,
        billed_seconds: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE calls
                SET cost = ?, currency = ?, billed_seconds = ?
                WHERE call_control_id = ?
                """,
                (cost, currency, billed_seconds, call_control_id),
            )

    def set_recording(
        self,
        call_control_id: str,
        file_path: str,
        content_type: str,
        recording_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO call_recordings (
                    call_control_id, file_path, content_type, created_at, recording_id
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(call_control_id) DO UPDATE SET
                    file_path = excluded.file_path,
                    content_type = excluded.content_type,
                    created_at = excluded.created_at,
                    recording_id = COALESCE(excluded.recording_id, call_recordings.recording_id)
                """,
                (call_control_id, file_path, content_type, _now(), recording_id),
            )

    def pending_provider_recording_deletions(self, limit: int = 20) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM provider_recording_deletions
                WHERE deleted_at IS NULL AND next_attempt_at <= ?
                ORDER BY next_attempt_at
                LIMIT ?
                """,
                (_now(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_provider_recording_deletion(self, recording_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE provider_recording_deletions
                SET deleted_at = ?, last_error = NULL
                WHERE recording_id = ?
                """,
                (_now(), recording_id),
            )

    def fail_provider_recording_deletion(self, recording_id: str, error: str) -> None:
        retry_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE provider_recording_deletions
                SET attempts = attempts + 1, next_attempt_at = ?, last_error = ?
                WHERE recording_id = ?
                """,
                (retry_at, error[:1000], recording_id),
            )

    def get_call(self, call_control_id: str) -> dict | None:
        with self._connect() as connection:
            call = connection.execute(
                "SELECT * FROM calls WHERE call_control_id = ?",
                (call_control_id,),
            ).fetchone()
            if call is None:
                return None
            transcript = connection.execute(
                """
                SELECT speaker, text, created_at
                FROM transcript_entries
                WHERE call_control_id = ?
                ORDER BY id
                """,
                (call_control_id,),
            ).fetchall()
            recording = connection.execute(
                """
                SELECT file_path, content_type
                FROM call_recordings
                WHERE call_control_id = ?
                """,
                (call_control_id,),
            ).fetchone()
            call_limit = connection.execute(
                """
                SELECT max_duration_seconds
                FROM call_limits
                WHERE call_control_id = ?
                """,
                (call_control_id,),
            ).fetchone()
            conversation = connection.execute(
                """
                SELECT conversation_id
                FROM call_conversations
                WHERE call_control_id = ?
                """,
                (call_control_id,),
            ).fetchone()
            opening = connection.execute(
                """
                SELECT opening_line
                FROM call_openings
                WHERE call_control_id = ?
                """,
                (call_control_id,),
            ).fetchone()
        result = dict(call)
        result["transcript"] = [dict(entry) for entry in transcript]
        result["recording_path"] = recording["file_path"] if recording else None
        result["recording_content_type"] = recording["content_type"] if recording else None
        result["max_duration_seconds"] = (
            call_limit["max_duration_seconds"] if call_limit else 300
        )
        result["conversation_id"] = conversation["conversation_id"] if conversation else None
        result["opening_line"] = opening["opening_line"] if opening else None
        return result

    def cleanup_expired(self, retention_days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._connect() as connection:
            recordings = connection.execute(
                """
                SELECT recording.file_path
                FROM call_recordings AS recording
                JOIN calls ON calls.call_control_id = recording.call_control_id
                WHERE calls.created_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for recording in recordings:
                Path(recording["file_path"]).unlink(missing_ok=True)
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_recording_deletions (
                    recording_id, next_attempt_at
                )
                SELECT recording.recording_id, ?
                FROM call_recordings AS recording
                JOIN calls ON calls.call_control_id = recording.call_control_id
                WHERE calls.created_at < ? AND recording.recording_id IS NOT NULL
                """,
                (_now(), cutoff),
            )
            cursor = connection.execute(
                "DELETE FROM calls WHERE created_at < ?",
                (cutoff,),
            )
        return cursor.rowcount
