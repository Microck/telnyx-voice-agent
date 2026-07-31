<p align="center">
  <img src="docs/logo.png" width="170" alt="telnyx-voice-agent logo">
</p>

<h1 align="center">telnyx-voice-agent</h1>

<p align="center">
  outbound voice agent with live operator approvals.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-black" alt="license"></a>
  <img src="https://img.shields.io/badge/python-3.11-black" alt="python 3.11">
  <img src="https://img.shields.io/badge/docker-compose-black" alt="docker compose">
</p>

---

places an outbound phone call through Telnyx Call Control, attaches a managed Telnyx AI Assistant after the recipient answers, and streams live approval requests to the operator via [ntfy](https://ntfy.sh) push notifications. the operator can approve, deny, or reply with information mid-conversation without blocking the call.

calls, transcripts, and dual-channel MP3 recordings persist in a private SQLite volume and expire after 30 days.

## start here

```bash
cp .env.example .env
cp config/personal-knowledge.example.md config/personal-knowledge.md
cp config/sensitive-knowledge.example.md config/sensitive-knowledge.md
```

fill `.env` with your Telnyx credentials and a generated `CALL_API_KEY`:

```bash
openssl rand -hex 32
```

deploy:

```bash
./scripts/deploy_docker.py
```

## place a call

```bash
curl --fail-with-body \
  -X POST http://127.0.0.1:48611/start-call \
  -H "Authorization: Bearer ${CALL_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+15555550101",
    "task": "Reserve a table for two tomorrow at 21:00.",
    "opening_line": "Hello, I am calling on behalf of Alex to reserve a table.",
    "language": "en-US"
  }'
```

read the result:

```bash
curl -H "Authorization: Bearer ${CALL_API_KEY}" \
  "http://127.0.0.1:48611/calls/${CALL_CONTROL_ID}"

curl -H "Authorization: Bearer ${CALL_API_KEY}" \
  "http://127.0.0.1:48611/calls/${CALL_CONTROL_ID}/transcript"
```

## how it works

```text
POST /start-call
  -> SQLite saves the call intent
  -> Telnyx Call Control dials with idempotency key + hard time limit
  -> call.answered starts recording and attaches Telnyx AI Assistant
  -> assistant handles speech, replies, and interruptions
  -> operator approval requests push via ntfy (25s window, 60s for final decisions)
  -> outcome, transcript, and recording persist locally
  -> call record expires after 30 days
```

## live approvals

the assistant can request operator input without blocking the conversation. subscribe to the private ntfy topic:

```bash
curl -H "Authorization: Bearer ${CALL_API_KEY}" \
  "http://127.0.0.1:48611/operator-notifications"
```

notifications provide approve, deny, and reply actions. a timeout never grants permission.

## pricing

all costs are pay-per-use from Telnyx. there is no markup from this project.

| Component | Rate |
| --- | --- |
| Voice AI Agent (STT + LLM + TTS) | $0.05/min |
| Outbound Call Control | $0.002/min + destination termination rate |
| Phone number rental | ~$1-2/month depending on country |

the termination rate depends on the destination carrier and prefix. check your Telnyx rate deck for exact rates. a typical 3-minute call to a European mobile costs roughly $0.18-0.24 total.

## configuration

| Variable | Purpose |
| --- | --- |
| `TELNYX_API_KEY` | Telnyx API v2 key |
| `TELNYX_CONNECTION_ID` | Call Control connection ID |
| `TELNYX_ASSISTANT_ID` | Managed AI Assistant ID |
| `TELNYX_PHONE_NUMBER` | Outbound caller ID |
| `TELNYX_PUBLIC_KEY` | Ed25519 public key for webhook verification |
| `OPERATOR_NAME` | Name used in conversation (defaults to "the operator") |
| `CALL_API_KEY` | Bearer token for the HTTP API |
| `PUBLIC_URL` | HTTPS origin reaching this container |
| `NTFY_TOPIC` | Private ntfy topic for operator notifications |

see `.env.example` for the full list including optional Cloudflare Tunnel support.

## endpoints

| Method | Path | Auth |
| --- | --- | --- |
| `POST` | `/start-call` | bearer |
| `GET` | `/calls/{id}` | bearer |
| `GET` | `/calls/{id}/transcript` | bearer |
| `GET` | `/calls/{id}/recording` | bearer |
| `POST` | `/call-events` | Telnyx Ed25519 signature |
| `POST` | `/assistant-outcome/{id}` | bearer + signature |
| `GET` | `/operator-notifications` | bearer |
| `GET` | `/health/live` | none |
| `GET` | `/health/ready` | none |

## security

- Ed25519 webhook signature verification with 5-minute replay window
- persistent event-ID deduplication
- bearer auth on all operator-facing endpoints
- single-use capability URLs for operator replies
- container runs non-root with read-only filesystem and dropped capabilities
- credentials stay outside the image via `.dockerignore`

## development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
python -m unittest discover -s tests -v
```

no real phone call runs as part of the test suite.

## license

[MIT](LICENSE)
