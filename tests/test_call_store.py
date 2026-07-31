import tempfile
import unittest
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.call_store import CallStore


class CallStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = CallStore(Path(self.temp_dir.name) / "calls.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_persists_call_outcome_transcript_and_cost(self):
        self.store.create_call(
            call_control_id="call-1",
            to_number="+34612345678",
            task="Reserve a table for two.",
            language="es-ES",
            max_duration_seconds=180,
            opening_line=(
                "Hola, soy el asistente de Marcos. Llamo para hacer una reserva."
            ),
        )
        self.store.append_transcript("call-1", "agent", "Buenos dias.")
        self.store.append_transcript("call-1", "recipient", "Una mesa para dos.")
        self.assertTrue(
            self.store.set_outcome("call-1", "success", "Table reserved for 21:00.")
        )
        self.assertFalse(
            self.store.set_outcome("call-1", "failed", "Must not overwrite.")
        )
        self.store.set_cost("call-1", 0.0488, "USD", 60)
        self.store.set_recording(
            "call-1",
            str(Path(self.temp_dir.name) / "call-1.mp3"),
            "audio/mpeg",
        )
        self.store.set_conversation_id("call-1", "conversation-1")
        self.store.replace_transcript(
            "call-1",
            [
                ("agent", "Buenos dias."),
                ("recipient", "Una mesa para dos."),
            ],
        )

        call = self.store.get_call("call-1")

        self.assertEqual(call["outcome_status"], "success")
        self.assertEqual(call["outcome"], "Table reserved for 21:00.")
        self.assertEqual(call["cost"], 0.0488)
        self.assertEqual(call["max_duration_seconds"], 180)
        self.assertEqual(call["recording_content_type"], "audio/mpeg")
        self.assertEqual(call["conversation_id"], "conversation-1")
        self.assertEqual(
            call["opening_line"],
            "Hola, soy el asistente de Marcos. Llamo para hacer una reserva.",
        )
        self.assertEqual(call["transcript"][0]["speaker"], "agent")
        self.assertEqual(call["transcript"][1]["speaker"], "recipient")

    def test_removes_calls_older_than_thirty_days(self):
        old_time = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        self.store.create_call(
            call_control_id="old-call",
            to_number="+15555550101",
            task="Ask for opening hours.",
            language="en-US",
            created_at=old_time,
        )

        self.assertEqual(self.store.cleanup_expired(30), 1)
        self.assertIsNone(self.store.get_call("old-call"))

    def test_cleanup_removes_the_recording_file(self):
        old_time = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        recording_path = Path(self.temp_dir.name) / "old-call.mp3"
        recording_path.write_bytes(b"recording")
        self.store.create_call(
            call_control_id="old-call",
            to_number="+15555550101",
            task="Ask for opening hours.",
            language="en-US",
            created_at=old_time,
        )
        self.store.set_recording(
            "old-call",
            str(recording_path),
            "audio/mpeg",
            recording_id="recording-old",
        )

        self.store.cleanup_expired(30)

        self.assertFalse(recording_path.exists())
        self.assertEqual(
            self.store.pending_provider_recording_deletions()[0]["recording_id"],
            "recording-old",
        )

    def test_operator_request_is_single_use(self):
        self.store.create_call(
            "call-approval",
            "+34612345678",
            "Cancel a booking.",
            "es-ES",
        )
        expires_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        self.store.create_operator_request(
            token_hash="token-hash",
            call_control_id="call-approval",
            kind="approval",
            question="Accept a EUR 10 fee?",
            proposed_action="Accept the cancellation fee.",
            expires_at=expires_at,
        )

        request = self.store.get_operator_request("token-hash")
        self.assertEqual(request["status"], "pending")
        self.assertTrue(
            self.store.complete_operator_request("token-hash", "approved", "Approved.")
        )
        self.assertFalse(
            self.store.complete_operator_request("token-hash", "denied", "Denied.")
        )

    def test_sensitive_approval_is_consumed_once(self):
        self.store.create_call(
            "call-identity",
            "+34612345678",
            "Make a reservation.",
            "es-ES",
        )
        self.store.create_operator_request(
            token_hash="dni-token",
            call_control_id="call-identity",
            kind="information",
            question="May I provide the DNI?",
            proposed_action=None,
            expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
            sensitive_field="dni",
            sensitive_reason="The venue requires identity verification.",
        )
        self.store.complete_operator_request("dni-token", "approved", "Approved.")

        self.assertTrue(
            self.store.consume_sensitive_approval("call-identity", "DNI")
        )
        self.assertFalse(
            self.store.consume_sensitive_approval("call-identity", "DNI")
        )

    def test_sensitive_approval_requires_an_exact_structured_field(self):
        self.store.create_call(
            "call-identity-text",
            "+34612345678",
            "Make a reservation.",
            "es-ES",
        )
        self.store.create_operator_request(
            token_hash="text-only-token",
            call_control_id="call-identity-text",
            kind="information",
            question="Do not disclose the DNI.",
            proposed_action=None,
            expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        )
        self.store.complete_operator_request(
            "text-only-token",
            "approved",
            "Approved.",
        )

        self.assertFalse(
            self.store.consume_sensitive_approval("call-identity-text", "dni")
        )

    def test_completed_status_is_terminal_and_answer_time_is_preserved(self):
        self.store.create_call("call-state", "+15555550101", "Ask.", "en-US")
        self.assertTrue(self.store.set_status("call-state", "answered"))
        first_answered_at = self.store.get_call("call-state")["answered_at"]
        self.assertFalse(self.store.set_status("call-state", "answered"))
        self.assertFalse(self.store.set_status("call-state", "ringing"))
        self.assertTrue(self.store.set_status("call-state", "completed"))
        self.assertFalse(self.store.set_status("call-state", "ringing"))

        call = self.store.get_call("call-state")
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["answered_at"], first_answered_at)

    def test_webhook_event_is_persistently_deduplicated(self):
        self.assertTrue(
            self.store.enqueue_webhook_event(
                "event-1",
                "2026-07-30T12:00:00Z",
                '{"data":{}}',
            )
        )
        self.assertFalse(
            self.store.enqueue_webhook_event(
                "event-1",
                "2026-07-30T12:00:00Z",
                '{"data":{}}',
            )
        )

    def test_empty_transcript_cannot_be_marked_final(self):
        self.store.create_call(
            "call-empty-transcript",
            "+15555550101",
            "Deliver a message.",
            "en-US",
        )

        self.assertFalse(
            self.store.replace_transcript(
                "call-empty-transcript",
                [],
                final=True,
            )
        )
        self.assertEqual(
            self.store.get_call("call-empty-transcript")["transcript_final"],
            0,
        )

    def test_transcript_requires_two_stable_snapshots_before_final(self):
        self.store.create_call(
            "call-settling",
            "+15555550101",
            "Deliver a message.",
            "en-US",
        )
        entries = [
            ("agent", "Did you understand?"),
            ("recipient", "Yes."),
        ]

        self.assertFalse(
            self.store.stage_final_transcript(
                "call-settling",
                entries,
                settle_seconds=0,
            )
        )
        self.assertTrue(
            self.store.stage_final_transcript(
                "call-settling",
                entries,
                settle_seconds=0,
            )
        )
        self.assertEqual(
            self.store.get_call("call-settling")["transcript_final"],
            1,
        )

    def test_call_request_is_reserved_before_dial_and_materialized_once(self):
        request = self.store.reserve_call_request(
            "request-1",
            "+34612345678",
            "Reserve a table.",
            "es-ES",
            60,
            "Hola.",
        )
        duplicate = self.store.reserve_call_request(
            "request-1",
            "+34612345678",
            "Reserve a table.",
            "es-ES",
            60,
            "Hola.",
        )

        self.assertEqual(request["status"], "pending")
        self.assertEqual(duplicate["request_id"], "request-1")
        self.assertTrue(
            self.store.materialize_call_request("request-1", "call-materialized")
        )
        self.assertTrue(
            self.store.materialize_call_request("request-1", "call-materialized")
        )
        self.assertEqual(
            self.store.get_call("call-materialized")["max_duration_seconds"],
            60,
        )

    def test_operator_decision_and_delivery_are_atomic(self):
        self.store.create_call(
            "call-delivery",
            "+34612345678",
            "Cancel a booking.",
            "es-ES",
        )
        self.store.create_operator_request(
            token_hash="delivery-token",
            call_control_id="call-delivery",
            kind="approval",
            question="Accept the fee?",
            proposed_action="Accept EUR 10.",
            expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        )

        self.assertTrue(
            self.store.complete_operator_request(
                "delivery-token",
                "approved",
                "Approved.",
                delivery_message="[OPERATOR RESPONSE] Approved.",
            )
        )
        deliveries = self.store.pending_operator_deliveries()
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["call_control_id"], "call-delivery")

    def test_validated_outcome_queues_one_durable_hangup(self):
        self.store.create_call(
            "call-finish",
            "+34612345678",
            "Deliver a message.",
            "es-ES",
        )
        self.store.set_status("call-finish", "answered")

        self.assertTrue(
            self.store.set_outcome_and_queue_hangup(
                "call-finish",
                "success",
                "Message understood.",
            )
        )
        self.assertFalse(
            self.store.set_outcome_and_queue_hangup(
                "call-finish",
                "failed",
                "Duplicate.",
            )
        )
        self.assertEqual(self.store.pending_hangups(), [])
        with sqlite3.connect(self.store.database_path) as connection:
            due_at = connection.execute(
                """
                SELECT next_attempt_at FROM hangup_requests
                WHERE call_control_id = ?
                """,
                ("call-finish",),
            ).fetchone()[0]
        self.assertGreaterEqual(
            datetime.fromisoformat(due_at),
            datetime.now(UTC) + timedelta(seconds=5),
        )

    def test_outcome_notification_is_claimed_once(self):
        self.store.create_call(
            "call-notification",
            "+34612345678",
            "Ask for opening hours.",
            "es-ES",
        )

        self.assertTrue(self.store.claim_outcome_notification("call-notification"))
        self.assertFalse(self.store.claim_outcome_notification("call-notification"))
        self.assertEqual(self.store.pending_outcome_notifications(), [])

        self.store.set_status("call-notification", "completed")
        self.store.replace_transcript(
            "call-notification",
            [("agent", "Goodbye.")],
            final=True,
        )

        self.assertEqual(
            self.store.pending_outcome_notifications(),
            ["call-notification"],
        )


if __name__ == "__main__":
    unittest.main()
