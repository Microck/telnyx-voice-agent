"""
FastAPI server for the AI Voice Agent.

Provides HTTP and WebSocket endpoints:
- GET  /           → Health check
- POST /start-call → Initiate outbound call via Twilio
- GET|POST /twiml  → Return TwiML to connect call to WebSocket (works for both inbound & outbound)
- POST /call-status → Receive call status updates from Twilio
- WS   /ws         → Handle Twilio Media Stream WebSocket connection

Inbound calls: Set your Twilio phone number's webhook to https://<ngrok-url>/twiml
Outbound calls: Hit POST /start-call — the webhook URL is passed programmatically
"""

import asyncio
import json
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import settings
from app.bot import run_bot
from app.services.twilio_service import make_outbound_call, generate_twiml_response
from app.utils.logger import (
    logger,
    log_call_ended,
    log_call_error,
)

app = FastAPI(
    title="AI Voice Agent",
    description="Real-time AI voice agent using Pipecat + Gemini Live + Twilio",
    version="1.0.0",
)


@app.get("/")
async def health_check():
    """Health check endpoint to verify the server is running."""
    return {
        "status": "ok",
        "service": "AI Voice Agent",
        "message": "Server is running. Use POST /start-call to initiate a call.",
    }

@app.post("/start-call")
async def start_call(request: Request):
    """
    Initiate an outbound phone call via Twilio.

    Optional JSON body:
    {
        "to_number": "+91XXXXXXXXXX"  // defaults to YOUR_PHONE_NUMBER from .env
    }
    """
    try:
        to_number = None
        try:
            body = await request.json()
            to_number = body.get("to_number")
        except Exception:
            pass 

        call_sid = make_outbound_call(to_number)

        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Call initiated successfully",
                "call_sid": call_sid,
                "to_number": to_number or settings.your_phone_number,
            },
        )

    except Exception as e:
        logger.error(f"Failed to start call: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "message": f"Failed to initiate call: {str(e)}",
            },
        )


# TwiML Endpoint (handles both inbound & outbound calls)
@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml_endpoint(request: Request):
    """
    Return TwiML XML that instructs Twilio to connect the call
    to our WebSocket endpoint via Media Streams.

    This endpoint is used for:
    - OUTBOUND calls: Twilio hits this when the called person answers
    - INBOUND calls: Twilio hits this when someone calls your Twilio number
      (requires webhook config in Twilio Console)
    """
    twiml = generate_twiml_response()
    direction = "INBOUND" if request.method == "GET" else "OUTBOUND/INBOUND"
    logger.info(f"📋 TwiML endpoint called ({direction}) — returning Media Stream connection instructions")
    return Response(content=twiml, media_type="application/xml")


@app.post("/call-status")
async def call_status(request: Request):
    """
    Receive call status updates from Twilio.

    Twilio sends POST requests here for: initiated, ringing, answered, completed
    """
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "unknown")
    call_status = form_data.get("CallStatus", "unknown")
    call_duration = form_data.get("CallDuration", "0")

    if call_status == "completed":
        log_call_ended(call_sid, float(call_duration))
    elif call_status in ("failed", "busy", "no-answer", "canceled"):
        log_call_error(call_sid, f"Call status: {call_status}")
    else:
        logger.info(f"📞 Call status update | SID: {call_sid} | Status: {call_status}")

    return PlainTextResponse("OK")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Handle the Twilio Media Stream WebSocket connection.

    This is where bidirectional audio streaming happens:
    - Twilio sends phone audio to us
    - We send AI audio responses back to Twilio
    """
    await websocket.accept()
    logger.info("🔌 WebSocket connection accepted from Twilio")

    stream_sid = None
    call_sid = None

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")
            if event == "start":
                stream_sid = data.get("streamSid")
                call_sid = data.get("start", {}).get("callSid")
                logger.info(f"Received Twilio start event | streamSid: {stream_sid} | callSid: {call_sid}")
                break
            elif event == "connected":
                logger.info("Received Twilio connected event")
            else:
                logger.debug(f"Received Twilio event: {event}")

        if not stream_sid:
            logger.error("Could not obtain streamSid from Twilio handshake")
            await websocket.close()
            return

        await run_bot(
            websocket=websocket,
            stream_sid=stream_sid,
            call_sid=call_sid,
        )

    except WebSocketDisconnect:
        logger.info("🔌 WebSocket disconnected by Twilio")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
        log_call_error(call_sid or "unknown", str(e))
    finally:
        logger.info("🔌 WebSocket connection closed")


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🚀 AI Voice Agent Server Starting")
    logger.info(f"   Host: {settings.host}")
    logger.info(f"   Port: {settings.port}")
    logger.info(f"   ngrok URL: {settings.ngrok_url}")
    logger.info(f"   WebSocket: {settings.websocket_url}")
    logger.info(f"   TwiML: {settings.twiml_url}")
    logger.info("")
    logger.info("📞 OUTBOUND CALL: curl -X POST http://localhost:8000/start-call")
    logger.info("")
    logger.info("📲 INBOUND CALL SETUP (optional):")
    logger.info(f"   Go to Twilio Console → Phone Numbers → Your Number")
    logger.info(f"   Set Voice webhook URL to: {settings.twiml_url}")
    logger.info(f"   Then call your Twilio number and the AI will answer!")
    logger.info("=" * 60)

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
        log_level="info",
    )
