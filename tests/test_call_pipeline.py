import unittest
from types import SimpleNamespace

from telnyx.types.call_dial_response import CallDialResponse, Data

import support  # noqa: F401

from app.config import settings
from app.prompts import (
    build_system_prompt,
    default_call_opening,
    extract_markdown_field,
    redact_sensitive_fields,
    select_call_language,
    spell_identifier_es,
)
from app.services.telnyx_service import (
    build_assistant_message_options,
    build_ai_start_options,
    build_assistant_override,
    build_dial_options,
    build_recording_options,
    conversation_messages_to_transcript,
    get_call_control_id,
)


class CallPipelineTests(unittest.TestCase):
    def test_reads_call_control_id_from_current_telnyx_response(self):
        response = CallDialResponse(
            data=Data(
                call_control_id="call-control-id",
                call_leg_id="call-leg-id",
                call_session_id="call-session-id",
                is_alive=False,
                record_type="call",
            )
        )

        self.assertEqual(get_call_control_id(response), "call-control-id")

    def test_system_prompt_is_spanish_first_and_contains_the_task(self):
        prompt = build_system_prompt(
            "Confirm the caller's appointment.",
            personal_knowledge="Operator name: Marcos",
        )

        self.assertIn("Spanish from Spain", prompt)
        self.assertIn("Confirm the caller's appointment.", prompt)
        self.assertIn("short, natural turns", prompt)
        self.assertIn("Operator name: Marcos", prompt)
        self.assertNotIn("TechNova", prompt)
        self.assertIn("The recipient is never Marcos", prompt)
        self.assertIn("Do not say the private question aloud", prompt)
        self.assertIn("Never say Marcos's surname", prompt)
        self.assertIn("already authorized", prompt)
        self.assertIn("Do not request approval merely to deliver", prompt)
        self.assertIn("Never say that you are waiting", prompt)
        self.assertIn("Never narrate internal state", prompt)

    def test_personal_knowledge_is_omitted_when_empty(self):
        prompt = build_system_prompt("Ask for opening hours.", personal_knowledge="")

        self.assertNotIn("Operator knowledge", prompt)

    def test_assistant_override_uses_language_voice_and_outcome_tool(self):
        override = build_assistant_override(
            "call-1",
            "Reserve a table for two.",
            "es-ES",
            "Operator name: Marcos",
        )

        self.assertEqual(override["id"], "assistant-test")
        self.assertIn("Reserve a table for two.", override["instructions"])
        self.assertEqual(override["tools"][0]["webhook"]["name"], "record_call_outcome")
        self.assertEqual(
            override["tools"][0]["webhook"]["headers"][0]["value"],
            f"Bearer {settings.tool_api_key}",
        )
        self.assertNotIn("hangup", [tool["type"] for tool in override["tools"]])
        tool_names = [
            tool["webhook"]["name"]
            for tool in override["tools"]
            if tool["type"] == "webhook"
        ]
        self.assertIn("request_operator_input", tool_names)
        self.assertIn("get_sensitive_identity", tool_names)
        approval_tool = next(
            tool
            for tool in override["tools"]
            if tool.get("webhook", {}).get("name") == "request_operator_input"
        )
        self.assertTrue(approval_tool["webhook"]["async"])

    def test_start_options_speak_the_opening_as_the_provider_greeting(self):
        opening = (
            "Hola, soy el asistente de Marcos. Llamo para hacer una segunda "
            "reserva para dos personas."
        )

        options = build_ai_start_options(
            "call-1",
            "Make a second reservation.",
            "es-ES",
            opening,
            "Operator name: Marcos",
        )

        self.assertEqual(options["greeting"], opening)
        self.assertFalse(
            options["interruption_settings"]["disable_greeting_interruption"]
        )
        self.assertIn(opening, options["assistant"]["instructions"])
        self.assertNotIn(
            "Wait silently for the recipient to speak first",
            options["assistant"]["instructions"],
        )

    def test_telnyx_commands_have_provider_limits_and_idempotency(self):
        dial = build_dial_options(
            "+34612345678",
            60,
            "request-1",
        )
        recording = build_recording_options("call-1", 60)
        message = build_assistant_message_options(
            "[OPERATOR RESPONSE] Approved.",
            "delivery-1",
        )

        self.assertEqual(dial["time_limit_secs"], 60)
        self.assertEqual(dial["command_id"], build_dial_options("+34612345678", 60, "request-1")["command_id"])
        self.assertTrue(dial["client_state"])
        self.assertEqual(recording["max_length"], 65)
        self.assertTrue(recording["command_id"])
        self.assertTrue(message["trigger_response"])
        self.assertEqual(
            [entry["role"] for entry in message["messages"]],
            ["system", "user"],
        )
        self.assertEqual(
            message["messages"][1]["metadata"]["source"],
            "voice-agent-control",
        )
        self.assertEqual(
            message["command_id"],
            build_assistant_message_options(
                "[OPERATOR RESPONSE] Approved.",
                "delivery-1",
            )["command_id"],
        )

    def test_default_opening_is_complete_and_identifies_marcos_only(self):
        self.assertEqual(
            default_call_opening("es-ES"),
            (
                "Hola, soy el asistente de Marcos. Llamo para hacer una gestión "
                "en su nombre. ¿Podría ayudarme?"
            ),
        )
        self.assertNotIn("Apellido", default_call_opening("es-ES"))

    def test_spanish_identifiers_are_spelled_one_character_at_a_time(self):
        self.assertEqual(
            spell_identifier_es("12345678Z"),
            "uno, dos, tres, cuatro, cinco, seis, siete, ocho, zeta",
        )

    def test_sensitive_markdown_returns_only_the_requested_field(self):
        document = "# Sensitive identity\n\n- DNI: 12345678Z\n"

        self.assertEqual(extract_markdown_field(document, "DNI"), "12345678Z")

    def test_outcome_notification_redacts_sensitive_values(self):
        document = (
            "# Sensitive identity\n\n"
            "- DNI: 12345678Z\n"
            "- Spoken DNI in Spanish: cinco, cuatro, seis\n"
        )

        self.assertEqual(
            redact_sensitive_fields(
                "Reserva confirmada con DNI 12345678Z.",
                document,
            ),
            "Reserva confirmada con DNI [redacted].",
        )

    def test_final_transcript_is_chronological(self):
        messages = [
            SimpleNamespace(
                role="assistant",
                text="Segundo.",
                created_at="2026-07-30T10:00:02Z",
            ),
            SimpleNamespace(
                role="user",
                text=None,
                content="Primero.",
                created_at="2026-07-30T10:00:01Z",
            ),
            SimpleNamespace(
                role="system",
                text="Internal.",
                created_at="2026-07-30T10:00:00Z",
            ),
        ]

        self.assertEqual(
            conversation_messages_to_transcript(messages),
            [("recipient", "Primero."), ("agent", "Segundo.")],
        )

    def test_webhook_message_dictionaries_are_supported(self):
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "¿Lo has entendido?"}],
                "created_at": "2026-07-30T10:00:01Z",
            },
            {
                "role": "user",
                "content": "Sí.",
                "created_at": "2026-07-30T10:00:02Z",
            },
        ]

        self.assertEqual(
            conversation_messages_to_transcript(messages),
            [("agent", "¿Lo has entendido?"), ("recipient", "Sí.")],
        )

    def test_synthetic_control_turn_is_not_reported_as_recipient_speech(self):
        messages = [
            {
                "role": "user",
                "content": "Continue the call now.",
                "metadata": {"source": "voice-agent-control"},
                "created_at": "2026-07-30T10:00:00Z",
            },
            {
                "role": "assistant",
                "content": "Hola, soy el asistente de Marcos.",
                "created_at": "2026-07-30T10:00:01Z",
            },
        ]

        self.assertEqual(
            conversation_messages_to_transcript(messages),
            [("agent", "Hola, soy el asistente de Marcos.")],
        )

    def test_language_defaults_from_destination_country(self):
        self.assertEqual(select_call_language("+34612345678"), "es-ES")
        self.assertEqual(select_call_language("+15555550101"), "en-US")
        self.assertEqual(select_call_language("+34612345678", "en-US"), "en-US")

if __name__ == "__main__":
    unittest.main()
