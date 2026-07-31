import base64
import json
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from support import PRIVATE_KEY  # noqa: E402

from app.main import (  # noqa: E402
    _call_report_id,
    _call_report_token,
    _operator_wait_seconds,
    app,
)
from app.config import settings  # noqa: E402
from app.services.telnyx_service import call_store  # noqa: E402


class SecurityTests(unittest.TestCase):
    client = TestClient(app)

    @staticmethod
    def _signed_tool_headers(body: bytes) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = PRIVATE_KEY.sign(timestamp.encode("ascii") + b"|" + body)
        return {
            "Authorization": f"Bearer {settings.tool_api_key}",
            "content-type": "application/json",
            "telnyx-timestamp": timestamp,
            "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
        }

    def test_api_schema_and_documentation_are_disabled(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_start_call_requires_bearer_token(self):
        response = self.client.post(
            "/start-call",
            json={
                "to_number": "+15555550101",
                "task": "Reserve a table for two.",
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_start_call_requires_a_task(self):
        response = self.client.post(
            "/start-call",
            headers={"Authorization": "Bearer test-call-api-key-32-characters-long"},
            json={"to_number": "+15555550101"},
        )

        self.assertEqual(response.status_code, 422)

    def test_start_call_rejects_duration_outside_allowed_range(self):
        headers = {"Authorization": "Bearer test-call-api-key-32-characters-long"}
        for duration in (29, 3601):
            with self.subTest(duration=duration):
                response = self.client.post(
                    "/start-call",
                    headers=headers,
                    json={
                        "to_number": "+15555550101",
                        "task": "Ask for opening hours.",
                        "max_duration_seconds": duration,
                    },
                )
                self.assertEqual(response.status_code, 422)

    def test_call_results_require_authentication(self):
        response = self.client.get("/calls/not-found")

        self.assertEqual(response.status_code, 401)

    def test_authenticated_missing_call_returns_not_found(self):
        response = self.client.get(
            "/calls/not-found",
            headers={"Authorization": "Bearer test-call-api-key-32-characters-long"},
        )

        self.assertEqual(response.status_code, 404)

    def test_recording_requires_authentication(self):
        response = self.client.get("/calls/not-found/recording")

        self.assertEqual(response.status_code, 401)

    def test_recording_endpoint_returns_saved_audio(self):
        call_id = "recording-test-call"
        recording_path = Path("/tmp/recording-test-call.mp3")
        recording_path.write_bytes(b"test recording")
        call_store.create_call(
            call_id,
            "+15555550101",
            "Ask for opening hours.",
            "en-US",
        )
        call_store.set_recording(call_id, str(recording_path), "audio/mpeg")

        response = self.client.get(
            f"/calls/{call_id}/recording",
            headers={"Authorization": "Bearer test-call-api-key-32-characters-long"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"test recording")
        recording_path.unlink()

    def test_private_call_report_shows_the_transcript(self):
        call_id = "report-test-call"
        recording_path = Path("/tmp/report-test-call.mp3")
        recording_path.write_bytes(b"report recording")
        call_store.create_call(
            call_id,
            "+34612345678",
            "Ask for opening hours.",
            "es-ES",
        )
        call_store.set_outcome(call_id, "success", "Open until 18:00.")
        call_store.set_recording(call_id, str(recording_path), "audio/mpeg")
        call_store.replace_transcript(
            call_id,
            [
                ("agent", "Buenas, llamo de parte de Marcos."),
                ("recipient", "Cerramos a las seis."),
            ],
        )
        call = call_store.get_call(call_id)
        report_id = _call_report_id(call_id)

        denied = self.client.get(
            f"/call-report/{report_id}",
            params={"token": "wrong-token"},
        )
        report = self.client.get(
            f"/call-report/{report_id}",
            params={"token": _call_report_token(call)},
        )
        recording = self.client.get(
            f"/call-report/{report_id}/recording",
            params={"token": _call_report_token(call)},
        )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.headers["cache-control"], "no-store")
        self.assertEqual(report.headers["x-frame-options"], "DENY")
        self.assertIn("Open until 18:00.", report.text)
        self.assertIn("Buenas, llamo de parte de Marcos.", report.text)
        self.assertIn("Cerramos a las seis.", report.text)
        self.assertIn("<audio controls", report.text)
        self.assertEqual(recording.status_code, 200)
        self.assertEqual(recording.content, b"report recording")
        recording_path.unlink()

    def test_webhook_rejects_missing_signature(self):
        response = self.client.post("/call-events", json={"data": {}})

        self.assertEqual(response.status_code, 401)

    def test_webhook_accepts_current_valid_signature(self):
        body = json.dumps(
            {"data": {"event_type": "call.initiated", "payload": {"call_control_id": "test-call"}}},
            separators=(",", ":"),
        ).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = PRIVATE_KEY.sign(timestamp.encode("ascii") + b"|" + body)

        response = self.client.post(
            "/call-events",
            content=body,
            headers={
                "content-type": "application/json",
                "telnyx-timestamp": timestamp,
                "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "OK")

    def test_webhook_rejects_expired_signature(self):
        body = b'{"data":{}}'
        timestamp = str(int(time.time()) - 301)
        signature = PRIVATE_KEY.sign(timestamp.encode("ascii") + b"|" + body)

        response = self.client.post(
            "/call-events",
            content=body,
            headers={
                "telnyx-timestamp": timestamp,
                "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_outcome_tool_requires_authentication_and_is_idempotent(self):
        call_id = "outcome-tool-test"
        call_store.create_call(call_id, "+15555550101", "Ask for hours.", "en-US")
        call_store.set_status(call_id, "answered")
        payload = {"status": "success", "summary": "Open until 18:00."}
        raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        self.assertEqual(
            self.client.post(f"/assistant-outcome/{call_id}", json=payload).status_code,
            401,
        )
        unsigned = self.client.post(
            f"/assistant-outcome/{call_id}",
            headers={"Authorization": f"Bearer {settings.tool_api_key}"},
            json=payload,
        )
        self.assertEqual(unsigned.status_code, 401)
        headers = self._signed_tool_headers(raw_payload)
        premature = self.client.post(
            f"/assistant-outcome/{call_id}", headers=headers, content=raw_payload
        )
        self.assertEqual(premature.status_code, 409)
        call_store.append_transcript(call_id, "recipient", "Yes, understood.")
        first = self.client.post(
            f"/assistant-outcome/{call_id}", headers=headers, content=raw_payload
        )
        second = self.client.post(
            f"/assistant-outcome/{call_id}", headers=headers, content=raw_payload
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["accepted"])
        self.assertFalse(second.json()["accepted"])

    def test_media_websocket_was_removed(self):
        self.assertNotIn("/ws", {route.path for route in app.routes})

    def test_operator_reply_token_is_not_a_public_listing(self):
        self.assertEqual(self.client.get("/operator-input/not-a-token").status_code, 404)
        self.assertEqual(
            self.client.get("/operator-input/not-a-token/approve").status_code,
            404,
        )

    def test_final_approval_waits_longer_after_four_minutes(self):
        recent = {"answered_at": datetime.now(UTC).isoformat()}
        long_call = {
            "answered_at": (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        }

        self.assertEqual(_operator_wait_seconds(recent, True), 25)
        self.assertEqual(_operator_wait_seconds(long_call, False), 25)
        self.assertEqual(_operator_wait_seconds(long_call, True), 60)


if __name__ == "__main__":
    unittest.main()
