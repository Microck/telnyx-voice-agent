"""
Telnyx service module for the AI Voice Agent.

Handles outbound call initiation via the Telnyx Call Control API.
"""

import base64
import hashlib
import json
import uuid

import telnyx

from app.config import settings
from app.call_store import CallStore
from app.prompts import (
    build_system_prompt,
    default_call_opening,
    load_personal_knowledge,
)
from app.utils.logger import log_call_started, log_call_error, logger


call_store = CallStore(settings.call_database_path)


def _command_id(value: str) -> str:
    """Map stable local identifiers to UUIDs accepted by Telnyx commands."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"voice.opendots.me:{value}"))


def build_dial_options(
    to_number: str,
    max_duration_seconds: int,
    request_id: str,
) -> dict:
    """Build the provider-enforced, idempotent Dial command."""
    client_state = base64.b64encode(
        json.dumps({"request_id": request_id}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return {
        "connection_id": settings.telnyx_connection_id,
        "to": to_number,
        "from_": settings.telnyx_phone_number,
        "webhook_url": settings.webhook_url,
        "client_state": client_state,
        "command_id": _command_id(f"dial:{request_id}"),
        "time_limit_secs": max_duration_seconds,
    }


def build_recording_options(
    call_control_id: str,
    max_duration_seconds: int,
) -> dict:
    """Bound recording storage independently of application uptime."""
    return {
        "channels": "dual",
        "format": "mp3",
        "recording_track": "both",
        "play_beep": False,
        "max_length": max_duration_seconds + 5,
        "command_id": _command_id(f"recording:{call_control_id}"),
        "client_state": base64.b64encode(call_control_id.encode("utf-8")).decode("ascii"),
    }


def build_assistant_message_options(message: str, delivery_id: str) -> dict:
    """Build an idempotent operator message that immediately resumes the agent."""
    return {
        "messages": [
            {"role": "system", "content": message},
            {
                "role": "user",
                "content": (
                    "Continue the live call now. Speak only to the recipient and "
                    "follow the latest system instruction."
                ),
                "metadata": {"source": "voice-agent-control"},
            },
        ],
        "trigger_response": True,
        "command_id": _command_id(f"operator:{delivery_id}"),
    }


def configure_telnyx():
    """Configure the Telnyx SDK with the API key."""
    telnyx.api_key = settings.telnyx_api_key


def get_call_control_id(call) -> str:
    """Read the identifier from the response shape used by the pinned SDK."""
    if call.data is None:
        raise RuntimeError("Telnyx dial response did not include call data.")
    return call.data.call_control_id


def make_outbound_call(
    to_number: str,
    task: str,
    language: str,
    max_duration_seconds: int = 300,
    opening_line: str | None = None,
    request_id: str | None = None,
) -> str:
    """
    Initiate an outbound phone call via Telnyx Call Control.

    The call is placed using the configured Connection ID. When answered,
    the webhook handler attaches the managed Telnyx AI Assistant.

    Args:
        to_number: Phone number to call. Defaults to YOUR_PHONE_NUMBER from .env

    Returns:
        The Call Control ID of the initiated call

    Raises:
        Exception: If the Telnyx API call fails
    """
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    resolved_request_id = request_id or str(uuid.uuid4())
    reserved = call_store.reserve_call_request(
        resolved_request_id,
        to_number,
        task,
        language,
        max_duration_seconds,
        opening_line,
    )
    if reserved["call_control_id"]:
        return reserved["call_control_id"]

    try:
        call = client.calls.dial(
            **build_dial_options(
                to_number,
                max_duration_seconds,
                resolved_request_id,
            )
        )

        call_control_id = get_call_control_id(call)
        call_store.materialize_call_request(resolved_request_id, call_control_id)
        log_call_started(call_control_id, to_number)
        logger.info(f"Call initiated successfully | Call Control ID: {call_control_id}")

        return call_control_id

    except Exception as e:
        call_store.fail_call_request(resolved_request_id, str(e))
        log_call_error("N/A", str(e))
        raise


def hangup_call(call_control_id: str) -> None:
    """End a call after the agent records its final outcome."""
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    client.calls.actions.hangup(call_control_id)


def start_call_recording(
    call_control_id: str,
    max_duration_seconds: int,
) -> None:
    """Start a private dual-channel MP3 recording for an answered call."""
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    client.calls.actions.start_recording(
        call_control_id,
        **build_recording_options(call_control_id, max_duration_seconds),
    )


def build_assistant_override(
    call_control_id: str,
    task: str,
    language: str,
    personal_knowledge: str,
    opening_line: str | None = None,
) -> dict:
    """Build the complete per-call assistant contract sent to Telnyx."""
    outcome_url = f"{settings.public_url}/assistant-outcome/{call_control_id}"
    operator_input_url = f"{settings.public_url}/assistant-operator-input"
    sensitive_identity_url = f"{settings.public_url}/assistant-sensitive-identity"
    authorization_header = [
        {
            "name": "Authorization",
            "value": f"Bearer {settings.tool_api_key}",
        }
    ]
    return {
        "id": settings.telnyx_assistant_id,
        "instructions": build_system_prompt(
            task,
            language,
            personal_knowledge,
            opening_line,
        ),
        "tools": [
            {
                "type": "webhook",
                "webhook": {
                    "name": "record_call_outcome",
                    "description": "Save the final factual result of this call.",
                    "url": outcome_url,
                    "method": "POST",
                    "headers": authorization_header,
                    "body_parameters": {
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["success", "partial", "failed"],
                                "description": "Whether the assigned task succeeded.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "A brief factual result with key details.",
                            },
                        },
                        "required": ["status", "summary"],
                    },
                },
            },
            {
                "type": "webhook",
                "webhook": {
                    "name": "request_operator_input",
                    "description": (
                        "Ask Marcos for an approval or missing fact. The result "
                        "will arrive later as a system message."
                    ),
                    "url": operator_input_url,
                    "method": "POST",
                    "async": True,
                    "headers": authorization_header,
                    "body_parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["approval", "information"],
                            },
                            "question": {
                                "type": "string",
                                "description": "One concise question for Marcos.",
                            },
                            "proposed_action": {
                                "type": "string",
                                "description": (
                                    "Exact action, price, and consequence. "
                                    "Leave empty for information requests."
                                ),
                            },
                            "final_step": {
                                "type": "boolean",
                                "description": (
                                    "True only when this blocks the final result "
                                    "after a long conversation."
                                ),
                            },
                            "sensitive_field": {
                                "type": "string",
                                "enum": ["dni"],
                                "description": (
                                    "Set only when requesting permission to "
                                    "disclose this exact sensitive field."
                                ),
                            },
                            "sensitive_reason": {
                                "type": "string",
                                "description": (
                                    "Why the recipient needs the sensitive "
                                    "field in this call."
                                ),
                            },
                        },
                        "required": ["kind", "question", "final_step"],
                    },
                },
            },
            {
                "type": "webhook",
                "webhook": {
                    "name": "get_sensitive_identity",
                    "description": (
                        "Retrieve Marcos's DNI only when directly requested and "
                        "necessary for legitimate identity verification."
                    ),
                    "url": sensitive_identity_url,
                    "method": "POST",
                    "headers": authorization_header,
                    "body_parameters": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string", "enum": ["dni"]},
                            "reason": {
                                "type": "string",
                                "description": "Why this field is necessary now.",
                            },
                        },
                        "required": ["field", "reason"],
                    },
                },
            },
        ],
    }


def start_ai_assistant(
    call_control_id: str,
    task: str,
    language: str,
    opening_line: str | None,
) -> str:
    """Attach the managed Telnyx AI Assistant to an answered call."""
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    personal_knowledge = load_personal_knowledge(settings.personal_knowledge_path)
    options = build_ai_start_options(
        call_control_id,
        task,
        language,
        opening_line,
        personal_knowledge,
    )
    response = client.calls.actions.start_ai_assistant(
        call_control_id,
        **options,
    )
    conversation_id = getattr(response, "conversation_id", None)
    if conversation_id is None and getattr(response, "data", None) is not None:
        conversation_id = getattr(response.data, "conversation_id", None)
    if not conversation_id:
        raise RuntimeError("Telnyx did not return an AI conversation ID.")
    return conversation_id


def build_ai_start_options(
    call_control_id: str,
    task: str,
    language: str,
    opening_line: str | None,
    personal_knowledge: str,
) -> dict:
    """Build the exact Telnyx start contract so greeting behavior is testable."""
    resolved_opening = opening_line or default_call_opening(language)
    return {
        "assistant": build_assistant_override(
            call_control_id,
            task,
            language,
            personal_knowledge,
            resolved_opening,
        ),
        # The provider greeting is the only opening path Telnyx guarantees will
        # become audible speech before the conversational model takes over.
        "greeting": resolved_opening,
        "voice": (
            settings.spanish_voice
            if language == "es-ES"
            else settings.english_voice
        ),
        "transcription": {
            "model": "deepgram/nova-3",
            "language": "es" if language == "es-ES" else "en",
        },
        "interruption_settings": {
            "enable": True,
            "disable_greeting_interruption": False,
        },
        "send_message_history_updates": True,
    }


def fetch_conversation_transcript(conversation_id: str) -> list[tuple[str, str]]:
    """Fetch the final transcript from Telnyx's conversation store."""
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    messages = []
    page_number = 1
    while True:
        response = client.ai.conversations.messages.list(
            conversation_id,
            page_size=100,
            page_number=page_number,
        )
        messages.extend(getattr(response, "data", response) or [])
        meta = getattr(response, "meta", None)
        total_pages = getattr(meta, "total_pages", None)
        if total_pages is None and isinstance(meta, dict):
            total_pages = meta.get("total_pages")
        if not total_pages or page_number >= int(total_pages):
            break
        page_number += 1
    return conversation_messages_to_transcript(messages)


_CONTROL_MESSAGE_PREFIX = (
    "Continue the live call now. Speak only to the recipient"
)


def conversation_messages_to_transcript(messages) -> list[tuple[str, str]]:
    """Normalize Telnyx's newest-first message list into spoken order."""
    def value(message, field):
        return message.get(field) if isinstance(message, dict) else getattr(message, field, None)

    def spoken_text(message) -> str | None:
        content = value(message, "text") or value(message, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                text = value(part, "text") or value(part, "content")
                if isinstance(text, str):
                    parts.append(text)
            return " ".join(parts) or None
        return None

    entries: list[tuple[str, str]] = []
    ordered_messages = sorted(
        messages,
        key=lambda message: str(value(message, "created_at") or ""),
    )
    for message in ordered_messages:
        role = value(message, "role")
        metadata = value(message, "metadata") or {}
        metadata_source = value(metadata, "source")
        if metadata_source == "voice-agent-control":
            continue
        text = spoken_text(message)
        if role not in {"assistant", "user"} or not text:
            continue
        if text.startswith(_CONTROL_MESSAGE_PREFIX):
            continue
        entries.append(("agent" if role == "assistant" else "recipient", text))
    return entries


def add_ai_assistant_message(
    call_control_id: str,
    message: str,
    delivery_id: str,
) -> None:
    """Inject an operator decision into an active Telnyx AI conversation."""
    client = telnyx.Telnyx(api_key=settings.telnyx_api_key)
    options = build_assistant_message_options(message, delivery_id)
    trigger_response = options.pop("trigger_response")
    client.calls.actions.add_ai_assistant_messages(
        call_control_id,
        **options,
        extra_body={"trigger_response": trigger_response},
    )
