"""
Twilio service module for the AI Voice Agent.

Handles outbound call initiation via the Twilio REST API.
"""

from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect

from app.config import settings
from app.utils.logger import log_call_started, log_call_error, logger


def get_twilio_client() -> Client:
    """Create and return a Twilio REST API client."""
    return Client(settings.twilio_account_sid, settings.twilio_auth_token)


def make_outbound_call(to_number: str = None) -> str:
    """
    Initiate an outbound phone call via Twilio.

    The call will connect to our TwiML endpoint, which instructs Twilio
    to open a WebSocket Media Stream to our server.

    Args:
        to_number: Phone number to call. Defaults to YOUR_PHONE_NUMBER from .env

    Returns:
        The Call SID of the initiated call

    Raises:
        Exception: If the Twilio API call fails
    """
    if to_number is None:
        to_number = settings.your_phone_number

    client = get_twilio_client()

    try:
        call = client.calls.create(
            to=to_number,
            from_=settings.twilio_phone_number,
            url=settings.twiml_url,
            method="POST",
            status_callback=f"{settings.ngrok_url}/call-status",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        log_call_started(call.sid, to_number)
        logger.info(f"📱 Call initiated successfully | SID: {call.sid}")

        return call.sid

    except Exception as e:
        log_call_error("N/A", str(e))
        raise


def generate_twiml_response() -> str:
    """
    Generate TwiML XML that instructs Twilio to connect the call
    to our WebSocket endpoint for Media Streams.

    Returns:
        TwiML XML string
    """
    response = VoiceResponse()

    # Connect the call to our WebSocket via Twilio Media Streams
    connect = Connect()
    connect.stream(
        url=settings.websocket_url,
        name="ai-voice-agent",
    )
    response.append(connect)

    logger.info(f"📋 TwiML generated | WebSocket URL: {settings.websocket_url}")

    return str(response)
