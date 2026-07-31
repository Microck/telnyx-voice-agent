#!/usr/bin/env python3
"""Start and inspect calls through the local voice-agent API."""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


DEFAULT_PROJECT = Path(__file__).resolve().parents[3]


def load_env(path: Path) -> dict[str, str]:
    """Read the simple KEY=VALUE entries used by this project's .env file."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def api_context() -> tuple[str, str]:
    project = Path(os.environ.get("VOICE_AGENT_PROJECT", DEFAULT_PROJECT))
    env = load_env(project / ".env")
    api_key = env.get("CALL_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"CALL_API_KEY is missing from {project / '.env'}")
    runtime_file = project / ".agent-runtime.json"
    if runtime_file.is_file():
        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            base_url = runtime.get("web", "")
            if base_url:
                return base_url, api_key
        except (json.JSONDecodeError, OSError):
            pass
    port = env.get("APP_PUBLIC_PORT", "48611")
    return f"http://127.0.0.1:{port}", api_key


def request_json(
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    base_url, api_key = api_context()
    headers = {"Authorization": f"Bearer {api_key}"}
    encoded_body = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded_body = json.dumps(body).encode("utf-8")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=encoded_body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Voice-agent API returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Voice-agent API is unavailable: {error.reason}") from error


def print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def start_call(args: argparse.Namespace) -> None:
    language = args.language or ("es-ES" if args.to.startswith("+34") else "en-US")
    payload = {
        "to_number": args.to,
        "task": args.task,
        "language": language,
        "max_duration_seconds": args.max_seconds,
    }
    if args.opening:
        payload["opening_line"] = args.opening
    if args.dry_run:
        print_json({"dry_run": True, "request": payload})
        return

    idempotency_key = args.idempotency_key or f"hermes-{uuid.uuid4()}"
    started = request_json(
        "/start-call",
        method="POST",
        body=payload,
        idempotency_key=idempotency_key,
    )
    if not args.wait:
        print_json(started)
        return

    call_id = started["call_control_id"]
    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        call = request_json(f"/calls/{urllib.parse.quote(call_id, safe='')}")
        terminal = call["status"] in {"completed", "failed"}
        transcript_ready = call.get("transcript_final") or not call.get("conversation_id")
        if terminal and transcript_ready:
            transcript = request_json(
                f"/calls/{urllib.parse.quote(call_id, safe='')}/transcript"
            )
            print_json({"started": started, "call": call, **transcript})
            return
        time.sleep(3)
    raise RuntimeError(
        f"Timed out waiting for {call_id}. Inspect it with the status command."
    )


def show_status(args: argparse.Namespace) -> None:
    encoded_id = urllib.parse.quote(args.call_control_id, safe="")
    call = request_json(f"/calls/{encoded_id}")
    if args.transcript:
        transcript = request_json(f"/calls/{encoded_id}/transcript")
        print_json({"call": call, **transcript})
        return
    print_json(call)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Place one outbound call")
    start.add_argument("--to", required=True, help="Destination in E.164 format")
    start.add_argument("--task", required=True, help="Concrete task for the agent")
    start.add_argument("--max-seconds", type=int, default=180)
    start.add_argument("--language", choices=("es-ES", "en-US"))
    start.add_argument("--opening")
    start.add_argument("--idempotency-key")
    start.add_argument("--wait", action="store_true")
    start.add_argument("--wait-timeout", type=int, default=420)
    start.add_argument("--dry-run", action="store_true")
    start.set_defaults(handler=start_call)

    status = commands.add_parser("status", help="Inspect an existing call")
    status.add_argument("call_control_id")
    status.add_argument("--transcript", action="store_true")
    status.set_defaults(handler=show_status)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.handler(args)
    except (KeyError, RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
