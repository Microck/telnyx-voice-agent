"""
FastAPI server for the AI Voice Agent.

Provides HTTP endpoints:
- GET  /             -> Health check
- POST /start-call   -> Initiate outbound call via Telnyx
- POST /call-events  -> Receive Telnyx Call Control webhooks

Inbound calls: Set your Telnyx Connection's webhook URL to https://<public-host>/call-events
Outbound calls: Hit POST /start-call -- the webhook URL is passed programmatically
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import secrets
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from urllib.parse import quote

import aiohttp
import telnyx
import uvicorn
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.config import settings
from app.prompts import (
    extract_markdown_field,
    redact_sensitive_fields,
    select_call_language,
    spell_identifier_es,
)
from app.services.telnyx_service import (
    call_store,
    add_ai_assistant_message,
    conversation_messages_to_transcript,
    fetch_conversation_transcript,
    hangup_call,
    make_outbound_call,
    start_ai_assistant,
    start_call_recording,
)
from app.utils.logger import (
    logger,
    log_call_ended,
    log_call_error,
)

_worker_task: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(_: FastAPI):
    global _worker_task
    _worker_task = asyncio.create_task(_durable_worker())
    try:
        yield
    finally:
        _worker_task.cancel()
        try:
            await _worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="AI Voice Agent",
    description="Outbound task caller using Telnyx AI Assistant",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_lifespan,
)


class StartCallRequest(BaseModel):
    """Validated boundary for an outbound call request."""

    model_config = ConfigDict(extra="forbid")

    to_number: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    language: Literal["es-ES", "en-US"] | None = None
    max_duration_seconds: int = Field(default=300, ge=30, le=3600)
    opening_line: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=300),
    ] = None


class AssistantOutcomeRequest(BaseModel):
    """Validated result reported by the Telnyx assistant tool."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success", "partial", "failed"]
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class OperatorInputRequest(BaseModel):
    """A concise approval or information request raised during a call."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["approval", "information"]
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    proposed_action: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, max_length=500),
    ] = None
    final_step: bool
    sensitive_field: Literal["dni"] | None = None
    sensitive_reason: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ] = None


class SensitiveIdentityRequest(BaseModel):
    """A narrowly scoped request for identity data kept outside the prompt."""

    model_config = ConfigDict(extra="forbid")

    field: Literal["dni"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


def _is_port_in_use(host: str, port: int) -> bool:
    """Return True when a TCP bind would fail because the port is already in use."""
    bind_host = host if host not in {"0.0.0.0", "::"} else "127.0.0.1"

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return sock.connect_ex((bind_host, port)) == 0
    finally:
        sock.close()


def _format_telnyx_error(error: Exception) -> tuple[int, dict[str, Any]]:
    """Convert Telnyx SDK exceptions into a clean API response payload."""
    status_code = 500
    message = str(error)
    telnyx_payload = {}

    if hasattr(error, "status_code"):
        try:
            status_code = int(error.status_code)
        except (TypeError, ValueError):
            status_code = 502

    response = getattr(error, "response", None)
    if response is not None and hasattr(response, "status_code"):
        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError):
            pass

    body = getattr(error, "body", None)
    if isinstance(body, dict):
        telnyx_payload = body
    elif isinstance(body, (bytes, bytearray)):
        try:
            telnyx_payload = json.loads(body.decode("utf-8"))
        except Exception:
            telnyx_payload = {}
    elif isinstance(body, str):
        try:
            loaded_body = json.loads(body)
            if isinstance(loaded_body, dict):
                telnyx_payload = loaded_body
        except Exception:
            telnyx_payload = {}
    elif isinstance(response, dict):
        telnyx_payload = response

    if telnyx_payload:
        errors = telnyx_payload.get("errors") or []
        if not isinstance(errors, list):
            errors = []

        details = errors
        if details and isinstance(details, list):
            first = details[0]
            if isinstance(first, dict):
                message = first.get("detail") or first.get("message") or message

                if first.get("code") == "90103":
                    message = "Telnyx dialing limit reached. Wait for your dial quota window to reset or reduce call attempts per hour."

        telnyx_error = telnyx_payload.get("telnyx_error") or {}
        if isinstance(telnyx_error, dict):
            code = telnyx_error.get("error_code")
            if code == "D60":
                message = (
                    "Telnyx blocked the outbound call: non-verified destinations are not allowed at this account level. "
                    "Verify destination numbers in Telnyx or upgrade the account level."
                )

    return status_code, {"status": "error", "message": message, "details": telnyx_payload}


@app.get("/")
async def health_check():
    """Health check endpoint to verify the server is running."""
    return {
        "status": "ok",
    }


@app.get("/health/live")
async def liveness_check():
    """Confirm that the HTTP process is responsive."""
    return {"status": "ok"}


@app.get("/health/ready")
async def readiness_check():
    """Confirm that startup configuration passed validation."""
    if not call_store.is_healthy() or (_worker_task is not None and _worker_task.done()):
        raise HTTPException(status_code=503, detail="Durable worker is unavailable.")
    return {"status": "ready"}


def _require_call_api_key(authorization: str | None) -> None:
    """Authorize the paid outbound-call operation."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token, settings.call_api_key):
        raise HTTPException(
            status_code=401,
            detail="A valid bearer token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _require_tool_api_key(authorization: str | None) -> None:
    """Authorize only Telnyx assistant tools, not paid call creation."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        token,
        settings.tool_api_key,
    ):
        raise HTTPException(status_code=401, detail="Invalid assistant tool token.")


def _operator_topic() -> str:
    """Return the stable private topic shared with the Discord bridge."""
    return settings.ntfy_topic


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _call_report_token(call: dict) -> str:
    """Sign a read-only report link that expires when retained call data is deleted."""
    message = f"report:{call['call_control_id']}:{call['created_at']}"
    return hmac.new(
        settings.report_signing_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _call_report_id(call_control_id: str) -> str:
    """Encode the provider call ID into a Cloudflare-safe path segment."""
    return base64.urlsafe_b64encode(call_control_id.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_call_report_id(report_id: str) -> str:
    padding = "=" * (-len(report_id) % 4)
    try:
        return base64.urlsafe_b64decode(report_id + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise HTTPException(status_code=404, detail="Call report not found.") from error


def _operator_wait_seconds(call: dict, final_step: bool) -> int:
    """Allow a longer final approval only after four minutes of conversation."""
    if not final_step or not call.get("answered_at"):
        return 25
    answered_at = datetime.fromisoformat(call["answered_at"])
    elapsed = datetime.now(UTC) - answered_at
    return 60 if elapsed >= timedelta(minutes=4) else 25


async def _inject_operator_result(call_control_id: str, message: str) -> None:
    delivery_id = str(uuid.uuid4())
    await asyncio.to_thread(
        add_ai_assistant_message,
        call_control_id,
        message,
        delivery_id,
    )


async def _expire_operator_request(token_hash: str, wait_seconds: int) -> None:
    await asyncio.sleep(wait_seconds)
    operator_request = call_store.get_operator_request(token_hash)
    if not operator_request or operator_request["status"] != "pending":
        return
    if call_store.complete_operator_request(
        token_hash,
        "expired",
        "Marcos did not respond before the approval window closed.",
    ):
        await _inject_operator_result(
            operator_request["call_control_id"],
            (
                "[OPERATOR RESPONSE] Marcos did not respond. Do not approve or "
                "invent the requested information. Tell the recipient that "
                "Marcos could not confirm it. Make at most one quick safe "
                "alternative attempt, then explain the result and say goodbye. "
                "Never end the call silently."
            ),
        )


async def _publish_operator_request(
    token: str,
    call: dict,
    body: OperatorInputRequest,
    wait_seconds: int,
) -> None:
    base_url = f"{settings.public_url}/operator-input/{token}"
    message = body.question
    if body.proposed_action:
        message += f"\nProposed action: {body.proposed_action}"
    message += f"\nReply within {wait_seconds} seconds."
    payload = {
        "topic": _operator_topic(),
        "title": f"Call approval: {call['to_number']}",
        "message": message,
        "priority": 5,
        "tags": ["telephone_receiver", "warning"],
        "actions": [
            {
                "action": "http",
                "label": "Approve",
                "url": f"{base_url}/approve",
                "method": "POST",
                "clear": True,
            },
            {
                "action": "http",
                "label": "Deny",
                "url": f"{base_url}/deny",
                "method": "POST",
                "clear": True,
            },
            {
                "action": "view",
                "label": "Reply",
                "url": base_url,
                "clear": True,
            },
        ],
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://ntfy.sh",
            json=payload,
            timeout=10,
        ) as response:
            response.raise_for_status()


def _safe_outcome_summary(summary: str) -> str:
    """Keep private identity values out of storage and push notifications."""
    if not settings.sensitive_knowledge_path.is_file():
        return summary
    sensitive_document = settings.sensitive_knowledge_path.read_text(encoding="utf-8")
    return redact_sensitive_fields(summary, sensitive_document)


async def _publish_call_outcome(call_control_id: str) -> None:
    """Send one concise private ntfy notification after a call ends."""
    call = call_store.get_call(call_control_id)
    if not call:
        return

    status = call.get("outcome_status") or "failed"
    summary = call.get("outcome") or "The call ended without a reported result."
    payload = {
        "topic": _operator_topic(),
        "title": f"Call finished: {status}",
        "message": (
            f"To: {call['to_number']}\n"
            f"Result: {summary[:500]}\n"
            f"Transcript: {len(call['transcript'])} turns\n"
            f"Recording: {'ready' if call.get('recording_path') else 'unavailable'}"
        ),
        "priority": 3 if status == "success" else 4,
        "tags": ["telephone_receiver", "white_check_mark" if status == "success" else "warning"],
        "actions": [
            {
                "action": "view",
                "label": "View transcript",
                "url": (
                    f"{settings.public_url}/call-report/"
                    f"{_call_report_id(call_control_id)}"
                    f"?token={_call_report_token(call)}"
                ),
                "clear": False,
            }
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://ntfy.sh",
                json=payload,
                timeout=10,
            ) as response:
                response.raise_for_status()
        logger.info(f"Outcome notification sent | Call Control ID: {call_control_id}")
        call_store.complete_outcome_notification(call_control_id)
    except Exception as error:
        call_store.fail_outcome_notification(call_control_id, str(error))
        logger.error(
            f"Could not send outcome notification for {call_control_id}: {error}"
        )


async def _get_detail_records(record_type: str, **filters: str) -> list[dict]:
    base_params = [(f"filter[{key}]", value) for key, value in filters.items()]
    base_params.append(("filter[record_type]", record_type))
    headers = {"Authorization": f"Bearer {settings.telnyx_api_key}"}
    records = []
    page_number = 1
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            params = [*base_params, ("page[number]", str(page_number)), ("page[size]", "100")]
            async with session.get(
                "https://api.telnyx.com/v2/detail_records",
                params=params,
                timeout=10,
            ) as response:
                response.raise_for_status()
                payload = await response.json()
            records.extend(payload.get("data") or [])
            total_pages = (payload.get("meta") or {}).get("total_pages") or 1
            if page_number >= int(total_pages):
                return records
            page_number += 1


async def _refresh_call_cost(call: dict) -> dict:
    """Load delayed Telnyx detail records when a completed call is read."""
    if call["status"] != "completed":
        return call
    try:
        call_control_id = call["call_control_id"]
        sip_records = await _get_detail_records(
            "sip-trunking",
            call_control_id=call_control_id,
        )
        session_records = []
        if sip_records and sip_records[0].get("telnyx_session_id"):
            session_id = sip_records[0]["telnyx_session_id"]
            # AI billing is split across the assistant session and its
            # inference, speech, recording, and Call Control child records.
            for record_type in (
                "call-control",
                "ai-voice-assistant",
                "inference",
                "speech-to-text",
                "text-to-speech",
                "recording",
            ):
                session_records.extend(
                    await _get_detail_records(
                        record_type,
                        telnyx_session_id=session_id,
                    )
                )
        records_by_id = {
            str(record.get("id") or hashlib.sha256(
                json.dumps(record, sort_keys=True).encode("utf-8")
            ).hexdigest()): record
            for record in [*sip_records, *session_records]
        }
        records = list(records_by_id.values())
        if records:
            cost = sum(
                (Decimal(str(record.get("cost") or "0")) for record in records),
                Decimal("0"),
            )
            billed_seconds = max(int(record.get("billed_sec") or 0) for record in records)
            currency = next(
                (record.get("currency") for record in records if record.get("currency")),
                "USD",
            )
            call_store.set_cost(call_control_id, float(cost), currency, billed_seconds)
            return call_store.get_call(call_control_id) or call
    except Exception as error:
        logger.warning(f"Could not refresh Telnyx cost for {call['call_control_id']}: {error}")
    return call


def _recording_call_control_id(payload: dict) -> str | None:
    """Resolve the original call from recording webhook client state."""
    client_state = payload.get("client_state")
    if not client_state:
        return None
    try:
        return base64.b64decode(client_state, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _call_request_id(payload: dict) -> str | None:
    """Recover the pre-dial intent from Dial client_state."""
    client_state = payload.get("client_state")
    if not client_state:
        return None
    try:
        decoded = base64.b64decode(client_state, validate=True).decode("utf-8")
        request_state = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    request_id = request_state.get("request_id")
    return request_id if isinstance(request_id, str) else None


async def _download_recording(
    call_control_id: str,
    recording_url: str,
    recording_id: str | None,
) -> None:
    """Copy a short-lived Telnyx recording into persistent private storage."""
    settings.recording_directory.mkdir(parents=True, exist_ok=True)
    file_name = hashlib.sha256(call_control_id.encode("utf-8")).hexdigest() + ".mp3"
    file_path = settings.recording_directory / file_name
    temporary_path = file_path.with_suffix(".tmp")
    max_recording_bytes = 100 * 1024 * 1024
    try:
        # Telnyx supplies a short-lived presigned storage URL. Adding an
        # Authorization header makes S3 reject it as two auth mechanisms.
        async with aiohttp.ClientSession() as session:
            async with session.get(recording_url, timeout=60) as response:
                response.raise_for_status()
                if int(response.headers.get("Content-Length") or 0) > max_recording_bytes:
                    raise RuntimeError("Recording exceeds the 100 MiB storage limit.")
                downloaded_bytes = 0
                with temporary_path.open("wb") as recording_file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded_bytes += len(chunk)
                        if downloaded_bytes > max_recording_bytes:
                            raise RuntimeError("Recording exceeds the 100 MiB storage limit.")
                        recording_file.write(chunk)
        temporary_path.replace(file_path)
        call_store.set_recording(
            call_control_id,
            str(file_path),
            "audio/mpeg",
            recording_id=recording_id,
        )
        logger.info(f"Recording saved | Call Control ID: {call_control_id}")
    except Exception as error:
        temporary_path.unlink(missing_ok=True)
        logger.error(f"Could not save recording for {call_control_id}: {error}")


async def _enforce_call_duration(call_control_id: str, max_duration_seconds: int) -> None:
    """Hang up an answered call when its configured hard limit expires."""
    await asyncio.sleep(max_duration_seconds)
    call = call_store.get_call(call_control_id)
    if not call or call["status"] == "completed":
        return
    if not call["outcome_status"]:
        call_store.set_outcome(
            call_control_id,
            "partial",
            f"Call reached its {max_duration_seconds}-second limit.",
        )
    try:
        await asyncio.to_thread(hangup_call, call_control_id)
        logger.info(f"Call duration limit reached | Call Control ID: {call_control_id}")
    except Exception as error:
        logger.error(f"Could not enforce duration limit for {call_control_id}: {error}")


def _verify_telnyx_webhook(raw_body: bytes, timestamp: str | None, signature: str | None) -> None:
    """Verify the raw Telnyx webhook body and reject replayed requests."""
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Telnyx signature headers.")

    try:
        sent_at = int(timestamp)
    except ValueError as error:
        raise HTTPException(status_code=401, detail="Invalid Telnyx timestamp.") from error

    if abs(int(time.time()) - sent_at) > settings.webhook_tolerance_seconds:
        raise HTTPException(status_code=401, detail="Expired Telnyx webhook.")

    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(settings.telnyx_public_key))
        public_key.verify(
            base64.b64decode(signature),
            timestamp.encode("ascii") + b"|" + raw_body,
        )
    except (ValueError, InvalidSignature) as error:
        raise HTTPException(status_code=401, detail="Invalid Telnyx signature.") from error


async def _verify_telnyx_tool_request(
    request: Request,
    timestamp: str | None,
    signature: str | None,
) -> None:
    """Apply the same raw-body signature contract to assistant tool callbacks."""
    _verify_telnyx_webhook(await request.body(), timestamp, signature)


@app.post("/start-call")
async def start_call(
    body: StartCallRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """
    Initiate an outbound phone call via Telnyx.

    JSON body:
    {
        "to_number": "+1XXXXXXXXXX",
        "task": "Reserve a table for two tomorrow at 21:00."
    }
    """
    _require_call_api_key(authorization)

    try:
        to_number = body.to_number

        if not to_number and not settings.your_phone_number:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "to_number is required. Include {\"to_number\": \"+1...\"} in request body or set YOUR_PHONE_NUMBER in .env",
                },
            )

        destination = to_number or settings.your_phone_number
        language = select_call_language(destination, body.language)
        blocker = call_store.call_start_blocker()
        if blocker:
            raise HTTPException(status_code=429, detail=blocker)
        request_id = idempotency_key or str(uuid.uuid4())
        if len(request_id) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key is too long.")
        call_control_id = await asyncio.to_thread(
            make_outbound_call,
            destination,
            body.task,
            language,
            body.max_duration_seconds,
            body.opening_line,
            request_id,
        )

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Call initiated successfully",
                "call_control_id": call_control_id,
                "request_id": request_id,
                "to_number": destination,
                "language": language,
                "max_duration_seconds": body.max_duration_seconds,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start call: {e}")

        # Telnyx errors do not expose a single stable base class across
        # SDK versions, so parse payload/status first.
        status_code, payload = _format_telnyx_error(e)
        if status_code != 500:
            return JSONResponse(
                status_code=status_code,
                content=payload,
            )

        permission_cls = getattr(telnyx, "PermissionDeniedError", None)
        if permission_cls is not None and isinstance(e, permission_cls):
            return JSONResponse(
                status_code=403,
                content=payload,
            )

        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Failed to initiate call: {str(e)}",
            },
        )


@app.get("/calls/{call_control_id}")
async def get_call(call_control_id: str, authorization: str | None = Header(default=None)):
    """Return the stored task result and billing summary."""
    _require_call_api_key(authorization)
    call = call_store.get_call(call_control_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    call = await _refresh_call_cost(call)
    call.pop("transcript", None)
    call["recording_available"] = bool(call.pop("recording_path", None))
    return call


@app.get("/calls/{call_control_id}/transcript")
async def get_call_transcript(
    call_control_id: str,
    authorization: str | None = Header(default=None),
):
    """Return the ordered transcript for one outbound call."""
    _require_call_api_key(authorization)
    call = call_store.get_call(call_control_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    return {
        "call_control_id": call_control_id,
        "transcript": call["transcript"],
    }


@app.get("/calls/{call_control_id}/recording")
async def get_call_recording(
    call_control_id: str,
    authorization: str | None = Header(default=None),
):
    """Return the private MP3 recording for one outbound call."""
    _require_call_api_key(authorization)
    call = call_store.get_call(call_control_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    recording_path = call.get("recording_path")
    if not recording_path or not os.path.isfile(recording_path):
        raise HTTPException(status_code=404, detail="Recording not available.")
    return FileResponse(
        recording_path,
        media_type=call["recording_content_type"],
        filename=f"{call_control_id}.mp3",
    )


@app.post("/assistant-outcome/{call_control_id}")
async def assistant_outcome(
    call_control_id: str,
    request: Request,
    body: AssistantOutcomeRequest,
    authorization: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
):
    """Accept the final result from the per-call Telnyx assistant tool."""
    _require_tool_api_key(authorization)
    await _verify_telnyx_tool_request(
        request,
        telnyx_timestamp,
        telnyx_signature_ed25519,
    )
    call = call_store.get_call(call_control_id)
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found.")
    if call["status"] != "answered":
        raise HTTPException(status_code=409, detail="The call is not active.")
    recipient_spoke = any(
        entry["speaker"] == "recipient" and entry["text"].strip()
        for entry in call["transcript"]
    )
    answered_at = datetime.fromisoformat(call["answered_at"])
    elapsed_seconds = (datetime.now(UTC) - answered_at).total_seconds()
    if body.status in {"success", "partial"} and not recipient_spoke:
        raise HTTPException(
            status_code=409,
            detail="No recipient reply is recorded. Continue the conversation.",
        )
    if body.status == "failed" and not recipient_spoke and elapsed_seconds < 15:
        raise HTTPException(
            status_code=409,
            detail=(
                "Initial silence is not a failure. Wait for the five-second "
                "opening, speak, and allow at least 15 seconds for a reply."
            ),
        )
    accepted = call_store.set_outcome_and_queue_hangup(
        call_control_id,
        body.status,
        _safe_outcome_summary(body.summary),
    )
    return {"accepted": accepted}


@app.get("/operator-notifications")
async def operator_notifications(
    authorization: str | None = Header(default=None),
):
    """Return the private ntfy topic the operator should subscribe to."""
    _require_call_api_key(authorization)
    topic = _operator_topic()
    return {
        "topic": topic,
        "subscribe_url": f"https://ntfy.sh/{topic}",
    }


@app.get("/call-report/{report_id}", response_class=HTMLResponse)
async def call_report(report_id: str, token: str):
    """Show one retained call through an unguessable read-only capability link."""
    call_control_id = _decode_call_report_id(report_id)
    call = call_store.get_call(call_control_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found.")
    if not hmac.compare_digest(token, _call_report_token(call)):
        raise HTTPException(status_code=401, detail="Invalid call report token.")

    transcript = call["transcript"]
    if transcript:
        transcript_html = "\n".join(
            (
                "<p><strong>"
                f"{'Assistant' if entry['speaker'] == 'agent' else 'Recipient'}:"
                "</strong> "
                f"{html.escape(entry['text'])}</p>"
            )
            for entry in transcript
        )
    else:
        transcript_html = (
            "<p>The transcript is still processing or was unavailable. "
            "Refresh this page shortly.</p>"
        )
    recording_html = ""
    if call.get("recording_path"):
        recording_url = (
            f"/call-report/{report_id}/recording"
            f"?token={token}"
        )
        recording_html = (
            "<h2>Recording</h2>"
            f'<audio controls preload="metadata" src="{html.escape(recording_url)}"></audio>'
            f'<p><a href="{html.escape(recording_url)}">Open audio</a></p>'
        )

    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call report</title>
<body>
<main>
<h1>Call report</h1>
<p><strong>To:</strong> {html.escape(call['to_number'])}</p>
<p><strong>Status:</strong> {html.escape(call['outcome_status'] or 'unknown')}</p>
<p><strong>Result:</strong> {html.escape(call['outcome'] or 'No result reported.')}</p>
{recording_html}
<h2>Transcript</h2>
{transcript_html}
</main>
</body>
</html>""",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; media-src 'self'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@app.get("/call-report/{report_id}/recording")
async def call_report_recording(report_id: str, token: str):
    """Stream retained call audio through the same signed report capability."""
    call_control_id = _decode_call_report_id(report_id)
    call = call_store.get_call(call_control_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found.")
    if not hmac.compare_digest(token, _call_report_token(call)):
        raise HTTPException(status_code=401, detail="Invalid call report token.")
    recording_path = call.get("recording_path")
    if not recording_path or not os.path.isfile(recording_path):
        raise HTTPException(status_code=404, detail="Recording not available.")
    return FileResponse(
        recording_path,
        media_type=call["recording_content_type"],
        headers={
            "Content-Disposition": 'inline; filename="call.mp3"',
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/assistant-operator-input")
async def assistant_operator_input(
    request: Request,
    body: OperatorInputRequest,
    authorization: str | None = Header(default=None),
    x_telnyx_call_control_id: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
):
    """Start an asynchronous, time-limited operator decision request."""
    _require_tool_api_key(authorization)
    await _verify_telnyx_tool_request(
        request,
        telnyx_timestamp,
        telnyx_signature_ed25519,
    )
    if not x_telnyx_call_control_id:
        raise HTTPException(status_code=400, detail="Missing Telnyx call ID.")
    call = call_store.get_call(x_telnyx_call_control_id)
    if not call or call["status"] != "answered":
        raise HTTPException(status_code=409, detail="The call is not active.")

    wait_seconds = _operator_wait_seconds(call, body.final_step)
    token = secrets.token_urlsafe(32)
    token_hash = _token_hash(token)
    expires_at = datetime.now(UTC) + timedelta(seconds=wait_seconds)
    call_store.create_operator_request(
        token_hash,
        x_telnyx_call_control_id,
        body.kind,
        body.question,
        body.proposed_action,
        expires_at.isoformat(),
        sensitive_field=body.sensitive_field,
        sensitive_reason=body.sensitive_reason,
    )
    await _inject_operator_result(
        x_telnyx_call_control_id,
        (
            "[OPERATOR INPUT PENDING] The recipient is not Marcos. The private "
            "tool question is for Marcos only. Do not repeat it aloud or imply "
            "an answer. If needed, say only that you are waiting for Marcos's "
            "confirmation. Continue the blocked action only after a system "
            "message marked [OPERATOR RESPONSE]."
        ),
    )
    try:
        await _publish_operator_request(token, call, body, wait_seconds)
    except Exception as error:
        call_store.complete_operator_request(
            token_hash,
            "delivery_failed",
            "The notification could not be delivered.",
        )
        await _inject_operator_result(
            x_telnyx_call_control_id,
            (
                "[OPERATOR RESPONSE] The notification could not be delivered. "
                "Do not approve the action. Tell the recipient Marcos could not "
                "be reached, try one quick safe alternative, then say goodbye."
            ),
        )
        raise HTTPException(
            status_code=502,
            detail="Could not deliver the operator notification.",
        ) from error

    return {"queued": True, "wait_seconds": wait_seconds}


@app.post("/assistant-sensitive-identity")
async def assistant_sensitive_identity(
    request: Request,
    body: SensitiveIdentityRequest,
    authorization: str | None = Header(default=None),
    x_telnyx_call_control_id: str | None = Header(default=None),
    telnyx_timestamp: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
):
    """Return DNI only to an authenticated tool on an active call."""
    _require_tool_api_key(authorization)
    await _verify_telnyx_tool_request(
        request,
        telnyx_timestamp,
        telnyx_signature_ed25519,
    )
    call = (
        call_store.get_call(x_telnyx_call_control_id)
        if x_telnyx_call_control_id
        else None
    )
    if not call or call["status"] != "answered":
        raise HTTPException(status_code=409, detail="The call is not active.")
    if not settings.sensitive_knowledge_path.is_file():
        raise HTTPException(status_code=404, detail="Sensitive identity is unavailable.")
    sensitive_document = settings.sensitive_knowledge_path.read_text(encoding="utf-8")
    value = extract_markdown_field(sensitive_document, "DNI")
    if not value:
        raise HTTPException(status_code=404, detail="DNI is unavailable.")
    if not call_store.consume_sensitive_approval(
        x_telnyx_call_control_id,
        body.field,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Marcos has not approved this exact DNI disclosure. Request "
                "operator input first and wait for [OPERATOR RESPONSE]."
            ),
        )
    return {
        "field": body.field,
        "value": value,
        "spoken_value_es": spell_identifier_es(value),
        "usage": (
            "In Spanish, say spoken_value_es exactly. Give it once. Do not "
            "repeat it unnecessarily or include it in the outcome summary."
        ),
    }


def _get_pending_operator_request(token: str) -> tuple[str, dict]:
    token_hash = _token_hash(token)
    operator_request = call_store.get_operator_request(token_hash)
    if not operator_request:
        raise HTTPException(status_code=404, detail="Request not found.")
    return token_hash, operator_request


async def _complete_operator_response(
    token: str,
    status: str,
    response: str,
) -> bool:
    token_hash, operator_request = _get_pending_operator_request(token)
    if datetime.now(UTC) >= datetime.fromisoformat(operator_request["expires_at"]):
        raise HTTPException(status_code=410, detail="This request has expired.")
    instruction = (
        f"[OPERATOR RESPONSE] Marcos replied: {response}. "
        "Apply this only to the exact request and genuinely similar actions "
        "with no higher price, risk, or consequence. Tell the recipient the "
        "decision and continue the assigned task."
    )
    return call_store.complete_operator_request(
        token_hash,
        status,
        response,
        delivery_message=instruction,
    )


@app.get("/operator-input/{token}", response_class=HTMLResponse)
async def operator_input_page(token: str):
    """Show a minimal private response form addressed by a one-time token."""
    _, operator_request = _get_pending_operator_request(token)
    if operator_request["status"] != "pending":
        raise HTTPException(status_code=410, detail="This request is already closed.")
    question = html.escape(operator_request["question"])
    action = html.escape(operator_request["proposed_action"] or "")
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call response</title>
<body>
<main>
<h1>Marcos's caller needs a reply</h1>
<p>{question}</p>
<p>{action}</p>
<form method="post" action="/operator-input/{token}/reply">
<label>Your reply <input name="reply" required maxlength="500" autofocus></label>
<button type="submit">Send</button>
</form>
</main>
</body>
</html>"""
    )


@app.post("/operator-input/{token}/approve")
async def approve_operator_input(token: str):
    accepted = await _complete_operator_response(
        token,
        "approved",
        "Approved.",
    )
    return {"accepted": accepted}


def _operator_confirmation_page(
    token: str,
    decision: Literal["approve", "deny"],
) -> HTMLResponse:
    """Require an explicit POST before a Discord link changes call authority."""
    _, operator_request = _get_pending_operator_request(token)
    if operator_request["status"] != "pending":
        raise HTTPException(status_code=410, detail="This request is already closed.")
    label = "Approve" if decision == "approve" else "Deny"
    question = html.escape(operator_request["question"])
    action = html.escape(operator_request["proposed_action"] or "")
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} call request</title>
<body>
<main>
<h1>{label} this request?</h1>
<p>{question}</p>
<p>{action}</p>
<form method="post" action="/operator-input/{token}/{decision}">
<button type="submit">{label}</button>
</form>
</main>
</body>
</html>"""
    )


@app.get("/operator-input/{token}/approve", response_class=HTMLResponse)
async def approve_operator_input_page(token: str):
    return _operator_confirmation_page(token, "approve")


@app.post("/operator-input/{token}/deny")
async def deny_operator_input(token: str):
    accepted = await _complete_operator_response(
        token,
        "denied",
        "Denied. Do not perform the proposed action.",
    )
    return {"accepted": accepted}


@app.get("/operator-input/{token}/deny", response_class=HTMLResponse)
async def deny_operator_input_page(token: str):
    return _operator_confirmation_page(token, "deny")


@app.post("/operator-input/{token}/reply", response_class=HTMLResponse)
async def reply_operator_input(
    token: str,
    reply: Annotated[
        str,
        Form(min_length=1, max_length=500),
    ],
):
    accepted = await _complete_operator_response(token, "answered", reply)
    message = "Reply sent." if accepted else "This request was already answered."
    return HTMLResponse(f"<p>{html.escape(message)}</p>")


async def _process_call_event(body: dict) -> None:
    """Apply one already-persisted Telnyx event."""
    event_data = body.get("data", {})
    event_type = event_data.get("event_type", "unknown")
    payload = event_data.get("payload", {})
    call_control_id = payload.get("call_control_id", "unknown")
    if call_store.get_call(call_control_id) is None:
        request_id = _call_request_id(payload)
        if request_id:
            call_store.materialize_call_request(request_id, call_control_id)

    logger.info(f"Telnyx event: {event_type} | Call Control ID: {call_control_id}")

    if event_type == "call.answered":
        call_store.set_status(call_control_id, "answered")
        call = call_store.get_call(call_control_id)
        try:
            if call:
                await asyncio.to_thread(
                    start_call_recording,
                    call_control_id,
                    call["max_duration_seconds"],
                )
            logger.info(f"Recording started | Call Control ID: {call_control_id}")
        except Exception as error:
            logger.error(f"Could not start recording for {call_control_id}: {error}")
        if call:
            try:
                conversation_id = await asyncio.to_thread(
                    start_ai_assistant,
                    call_control_id,
                    call["task"],
                    call["language"],
                    call["opening_line"],
                )
                call_store.set_conversation_id(call_control_id, conversation_id)
                logger.info(
                    "Telnyx AI Assistant started | "
                    f"Call Control ID: {call_control_id} | Conversation ID: {conversation_id}"
                )
            except Exception as error:
                call_store.set_outcome(
                    call_control_id,
                    "failed",
                    f"Telnyx AI Assistant could not start: {error}",
                )
                logger.error(f"Could not start Telnyx AI for {call_control_id}: {error}")
                try:
                    await asyncio.to_thread(hangup_call, call_control_id)
                except Exception as hangup_error:
                    logger.error(f"Could not hang up failed AI call {call_control_id}: {hangup_error}")

    elif event_type == "call.hangup":
        call = call_store.get_call(call_control_id)
        hangup_source = payload.get("hangup_source", "unknown")
        hangup_cause = payload.get("hangup_cause", "unknown")
        sip_hangup_cause = payload.get("sip_hangup_cause", "unknown")
        if call and not call["outcome_status"]:
            call_store.set_outcome(
                call_control_id,
                "failed",
                (
                    "Telnyx reported that the call ended "
                    f"(source={hangup_source}, cause={hangup_cause}, "
                    f"sip_cause={sip_hangup_cause})."
                ),
                source="fallback",
            )
        call_store.set_status(call_control_id, "completed")
        call_store.claim_outcome_notification(call_control_id)
        log_call_ended(call_control_id)
        logger.info(
            "Call ended | "
            f"Source: {hangup_source} | Cause: {hangup_cause} | "
            f"SIP cause: {sip_hangup_cause}"
        )

    elif event_type == "call.recording.saved":
        recording_call_id = _recording_call_control_id(payload)
        recording_url = (payload.get("recording_urls") or {}).get("mp3")
        if recording_call_id and recording_url:
            await _download_recording(
                recording_call_id,
                recording_url,
                payload.get("recording_id"),
            )
        else:
            logger.error("Recording webhook did not contain usable client state and MP3 URL.")

    elif event_type == "call.conversation.ended":
        conversation_id = payload.get("conversation_id")
        if conversation_id:
            call_store.set_conversation_id(call_control_id, conversation_id)
            try:
                transcript = await asyncio.to_thread(
                    fetch_conversation_transcript,
                    conversation_id,
                )
                if call_store.stage_final_transcript(
                    call_control_id,
                    transcript,
                ):
                    logger.info(f"Transcript saved | Call Control ID: {call_control_id}")
                else:
                    logger.info(
                        "Transcript not ready; durable worker will retry | "
                        f"Call Control ID: {call_control_id}"
                    )
            except Exception as error:
                logger.error(f"Could not fetch transcript for {call_control_id}: {error}")

    elif event_type == "call.ai_gather.message_history_updated":
        message_history = payload.get("message_history") or payload.get("messages")
        if isinstance(message_history, list):
            transcript = conversation_messages_to_transcript(message_history)
            if transcript:
                call_store.replace_transcript(call_control_id, transcript)

    elif event_type in ("call.initiated", "call.ringing"):
        call_store.set_status(call_control_id, event_type.removeprefix("call."))
        logger.info(f"Call status: {event_type}")


async def _process_pending_work_once() -> None:
    """Drain durable webhook and operator-delivery work after restarts."""
    call_store.expire_operator_requests()
    for request in call_store.pending_hangups():
        call_control_id = request["call_control_id"]
        call = call_store.get_call(call_control_id)
        if not call or call["status"] in {"completed", "failed"}:
            call_store.complete_hangup(call_control_id)
            continue
        try:
            await asyncio.to_thread(hangup_call, call_control_id)
            call_store.complete_hangup(call_control_id)
        except Exception as error:
            call_store.fail_hangup(call_control_id, str(error))
    for call in call_store.calls_needing_final_transcript():
        try:
            transcript = await asyncio.to_thread(
                fetch_conversation_transcript,
                call["conversation_id"],
            )
            call_store.stage_final_transcript(
                call["call_control_id"],
                transcript,
            )
        except Exception as error:
            call_store.defer_final_transcript(call["call_control_id"])
            logger.warning(
                "Could not finalize transcript for "
                f"{call['call_control_id']}: {error}"
            )
    for call_control_id in call_store.pending_outcome_notifications():
        await _publish_call_outcome(call_control_id)
    for deletion in call_store.pending_provider_recording_deletions():
        recording_id = deletion["recording_id"]
        try:
            headers = {"Authorization": f"Bearer {settings.telnyx_api_key}"}
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.delete(
                    f"https://api.telnyx.com/v2/recordings/{quote(recording_id, safe='')}",
                    timeout=10,
                ) as response:
                    if response.status not in {200, 204, 404}:
                        response.raise_for_status()
            call_store.complete_provider_recording_deletion(recording_id)
        except Exception as error:
            call_store.fail_provider_recording_deletion(recording_id, str(error))
    for delivery in call_store.pending_operator_deliveries():
        call = call_store.get_call(delivery["call_control_id"])
        if not call or call["status"] in {"completed", "failed"}:
            call_store.complete_operator_delivery(delivery["id"])
            continue
        try:
            await asyncio.to_thread(
                add_ai_assistant_message,
                delivery["call_control_id"],
                delivery["message"],
                str(delivery["id"]),
            )
            call_store.complete_operator_delivery(delivery["id"])
        except Exception as error:
            call_store.fail_operator_delivery(delivery["id"], str(error))

    for event in call_store.pending_webhook_events():
        try:
            await _process_call_event(json.loads(event["raw_body"]))
            call_store.complete_webhook_event(event["event_id"])
        except Exception as error:
            call_store.fail_webhook_event(event["event_id"], str(error))
            logger.error(f"Could not process Telnyx event {event['event_id']}: {error}")


async def _durable_worker() -> None:
    """Continuously resume persisted work independently of request lifetimes."""
    next_cleanup_at = 0.0
    while True:
        await _process_pending_work_once()
        if time.monotonic() >= next_cleanup_at:
            call_store.cleanup_expired(settings.call_retention_days)
            next_cleanup_at = time.monotonic() + 3600
        await asyncio.sleep(1)


@app.post("/call-events")
async def call_events(
    request: Request,
    telnyx_timestamp: str | None = Header(default=None),
    telnyx_signature_ed25519: str | None = Header(default=None),
):
    """Persist a verified Telnyx webhook and acknowledge it promptly."""
    raw_body = await request.body()
    _verify_telnyx_webhook(raw_body, telnyx_timestamp, telnyx_signature_ed25519)
    body = json.loads(raw_body)
    event_data = body.get("data", {})
    event_id = event_data.get("id") or hashlib.sha256(raw_body).hexdigest()
    call_store.enqueue_webhook_event(
        event_id,
        event_data.get("occurred_at"),
        raw_body.decode("utf-8"),
    )
    return PlainTextResponse("OK")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("AI Voice Agent Server Starting")
    logger.info(f"   Host: {settings.host}")
    logger.info(f"   Port: {settings.port}")
    logger.info(f"   Public URL: {settings.public_url}")
    logger.info(f"   Webhook: {settings.webhook_url}")
    logger.info("")
    logger.info("OUTBOUND CALL: curl -X POST http://localhost:8000/start-call")
    logger.info("=" * 60)

    if _is_port_in_use(settings.host, settings.port):
        logger.error(f"Port {settings.port} is already in use on {settings.host}.")
        logger.error("Stop the existing process bound to 8000, then restart this service.")
        raise SystemExit(1)

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=os.getenv("APP_RELOAD", "false").lower() in {"1", "true", "yes", "on"},
        log_level="info",
        access_log=False,
    )
