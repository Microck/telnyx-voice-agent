---
name: place-phone-call
description: Place and monitor outbound phone calls through the local AI voice agent. Use when the operator asks to call a number, make a reservation by phone, request information, deliver a message, dispute a purchase, or complete another task by phone.
---

# Place Phone Call

Use the bundled `scripts/voice-call.py` command. It talks only to the local
service in `<project-root>`.

## 1. Prepare the call

Resolve these facts from the request and recent conversation:

- Destination number in E.164 form, such as `+34612345678`
- One concrete task with the desired result
- Material facts the recipient will need
- Maximum call length

Ask one concise question only when a missing fact can change the result. Treat
an explicit request such as "call", "phone", or "try again" as authorization
to place that call. A hypothetical question or draft is not authorization.

Use Spanish from Spain for `+34` numbers and English for other country codes,
unless the operator specifies another language. Use these cost-aware limits:

- 90 seconds for a short message or simple question
- 180 seconds for a reservation or routine request
- 300 seconds for a dispute or complex task

Preserve exact wording when the operator says to say something exactly. Otherwise,
write a concise task that states the goal and completion condition.

Before calling, analyze the task from the recipient's point of view. List the
facts the recipient is likely to need in order to complete the request, and
ask the operator for any missing facts that could block completion. Ask the questions
before the readiness check and before any paid call. Keep the questions concise
and group them into one message when possible.

For appointments, check for likely requirements such as:

- Exact service or treatment requested
- Current condition, prior treatment, or relevant technical details
- Preferred date and time, including acceptable alternatives
- Full name and contact details if the business needs them
- Date of birth or other identity details only when the recipient is likely to
  require them
- Price, deposit, cancellation, or other material terms the caller should
  confirm

Do not invent answers, infer sensitive personal data, or disclose details that
the operator has not provided. If a question is optional or unlikely to block the
task, let the call agent ask it unless asking in advance would materially
improve the chance of success. If the operator does not answer a blocking question,
do not place the call; explain what is missing.

## 2. Check the local service before spending money

Before the first real call, verify that the configured API is ready. The call
script reads `APP_PUBLIC_PORT` from `<project-root>/.env`, so
use the same port rather than assuming a default:

```bash
port=$(grep '^APP_PUBLIC_PORT=' <project-root>/.env | cut -d= -f2)
port=${port:-48611}
curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/health/ready"
```

If readiness fails, inspect the container before retrying:

```bash
docker compose ps ai-voice-agent
docker compose logs --tail=80 ai-voice-agent
```

If the container is running but unhealthy, and there is no active call, restart
only that service and wait for readiness:

```bash
docker compose restart ai-voice-agent
until curl --fail --silent --show-error --max-time 5 "http://127.0.0.1:${port}/health/ready"; do sleep 2; done
```

A `RemoteDisconnected`, connection reset, or empty HTTP response from the call
script usually means the local container is unhealthy. Do not retry the paid
call until the readiness endpoint returns successfully.

## 3. Preview when details are uncertain

Run a dry preview:

```bash
python skills/place-phone-call/scripts/voice-call.py start \
  --to '+34612345678' \
  --task 'Reserve a table for two on 1 August at 20:00.' \
  --max-seconds 180 \
  --dry-run
```

The preview is complete when its JSON contains the correct number, task,
language, and time limit. A preview never places a call.

## 4. Place one call

Use `--wait` so the command returns the final call record and transcript:

```bash
python skills/place-phone-call/scripts/voice-call.py start \
  --to '+34612345678' \
  --task 'Reserve a table for two on 1 August at 20:00.' \
  --max-seconds 180 \
  --wait
```

Add `--language es-ES` or `--language en-US` only to override the country-code
default. Add `--opening` only when the operator supplied custom opening words.

The command creates one idempotency key. If it returns a `call_control_id`, do
not start another call unless the operator explicitly asks. If the command is
interrupted, check that call before considering a retry:

```bash
python skills/place-phone-call/scripts/voice-call.py status \
  '<call_control_id>'
```

## 5. Report the result

State whether the recipient answered and whether the task succeeded. Keep the
summary short. The service sends the final summary, transcript count,
recording status, and private report link through ntfy and Discord after the
transcript settles.

Never place a follow-up call automatically. Wait for a new explicit request.

