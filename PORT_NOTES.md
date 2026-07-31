# Twilio to Telnyx Port Notes

## What Changed

- **app/config.py**: Replaced `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` with `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_PHONE_NUMBER`. Removed the `twiml_url` property; added `webhook_url` property pointing to `/call-events`.
- **app/bot.py**: Swapped `TwilioFrameSerializer` for `TelnyxFrameSerializer` from `pipecat.serializers.telnyx`. The Telnyx serializer takes `stream_id`, `outbound_encoding`, `inbound_encoding`, `call_control_id`, and `api_key`.
- **app/main.py**: Removed the `/twiml` endpoint (Telnyx does not use TwiML). Replaced `/call-status` with `/call-events` which handles Telnyx Call Control webhook events (JSON-based, not form-encoded). On `call.answered`, we issue a `streaming_start` command to open the WebSocket media stream. The `/ws` endpoint now reads Telnyx's `stream_id` and `call_control_id` from the start event.
- **app/services/twilio_service.py**: Deleted.
- **app/services/telnyx_service.py**: Created. Uses the Telnyx Python SDK to place outbound calls via Call Control API.
- **requirements.txt**: `twilio` replaced with `telnyx`; Pipecat uses the supported `[google,websocket]` extras and the Telnyx SDK is installed directly.
- **.env.example**: Updated variable names.

## Assumptions

1. **Telnyx Connection ID**: Telnyx routes calls through "Connections" (TeXML apps, Call Control apps, or FQDN connections). This port assumes a Call Control Connection. The `TELNYX_CONNECTION_ID` env var should be the UUID of a Call Control Connection configured in the Telnyx portal.

2. **Media streaming initiation**: Telnyx does not auto-stream media like Twilio's TwiML `<Connect><Stream>`. Instead, the app issues an authenticated, bidirectional `streaming_start` command on the signed `call.answered` webhook event.

3. **WebSocket start event format**: The Telnyx media stream WebSocket sends a `start` event with `stream_id` at the top level and `call_control_id` inside `start` payload. This matches the Telnyx media streaming documentation pattern. If the actual field names differ at runtime, adjust the keys in `websocket_endpoint`.

4. **Audio encoding**: Defaulted to PCMU (G.711 u-law) for both inbound and outbound, which is the most common telephony encoding and matches what the Pipecat Telnyx serializer supports.

5. **Auto hang-up**: The Telnyx serializer's `auto_hang_up` feature is enabled by default. It uses the Telnyx REST API to terminate calls when the pipeline sends an EndFrame or CancelFrame. This requires `call_control_id` and `api_key` to be passed to the serializer.

6. **Inbound calls**: For inbound calls, configure your Telnyx Connection's webhook URL to point to `https://<ngrok-url>/call-events`. The `call.answered` handler will start media streaming just as it does for outbound calls.

7. **stream_track**: Set to `"both_tracks"` so the AI hears the caller and the caller hears the AI. Adjust to `"inbound_track"` if you only need one direction.

## Setup Steps

1. Create a Telnyx account and get an API key.
2. Purchase a phone number on Telnyx.
3. Create a Call Control Connection in the Telnyx portal.
4. Assign your phone number to the Connection.
5. Set the Connection's webhook URL to your stable `PUBLIC_URL` + `/call-events`.
6. Copy `.env.example` to `.env` and fill in the values.
7. Run `pip install --require-hashes -r requirements.lock`.
8. Start the server: `python -m app.main`
