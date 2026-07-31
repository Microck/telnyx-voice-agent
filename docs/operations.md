# Operations

## Call flow

```text
POST /start-call
  -> SQLite saves the call intent before dialing
  -> Telnyx Call Control dials with an idempotency key and hard time limit
  -> call.answered starts recording and Telnyx AI
  -> Telnyx handles speech, replies, and interruptions
  -> record_call_outcome saves the result
  -> hangup ends the call
  -> conversation and recording webhooks persist the transcript and MP3
```

The destination language defaults to Spain Spanish for `+34` numbers and
English for all other numbers. Each request can override the language and set a
hard call limit from 30 to 3600 seconds. A request can also provide a complete
`opening_line`. The assistant waits for the recipient to speak, then uses this
line to introduce Marcos and the task before normal conversation begins. If no
recipient speech is detected within five seconds, the durable worker triggers
the opening instead. Final transcripts require two matching Telnyx snapshots
at least ten seconds apart so a late final reply is not lost.

## Private data

`config/personal-knowledge.md` is mounted read-only and injected into each
call's instructions. The assistant must reveal only facts needed for its task.
`config/sensitive-knowledge.md` is mounted separately and is never injected
into the prompt. The assistant can retrieve it only through the restricted
identity tool during an active call.

SQLite data and MP3 recordings live in the private `call-data` Docker volume.
A durable worker processes webhook retries, operator replies, notifications,
and retention after restarts. The default retention period is 30 days. The
worker also queues deletion of expired Telnyx-hosted recording copies.

## Security

- `/start-call`, call results, transcripts, and recordings require
  `CALL_API_KEY`. Assistant tools use a separate derived credential.
- `/call-events` and assistant tools verify the Telnyx Ed25519 signature and
  timestamp. Webhook event IDs are persistently deduplicated.
- Operator replies use random, single-use capability URLs. Timeout always
  denies authority.
- The service binds to localhost. Cloudflare Tunnel provides the public route.
- Docker uses a read-only root filesystem, dropped capabilities, and
  `restart: unless-stopped`.

## Verification

```bash
./scripts/deploy_docker.py --status
curl --fail https://voice.example.com/health/ready
docker compose logs --tail 100 ai-voice-agent cloudflared
```

Automated tests never place a real phone call.
