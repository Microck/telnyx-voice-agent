#!/usr/bin/env python3
"""Build and run the production Docker Compose deployment."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_DIR / ".env"
RUNTIME_FILE = PROJECT_DIR / ".agent-runtime.json"
PRIVATE_CONFIG_FILES = (
    PROJECT_DIR / "config" / "personal-knowledge.md",
    PROJECT_DIR / "config" / "sensitive-knowledge.md",
)
REQUIRED_ENV = (
    "TELNYX_API_KEY",
    "TELNYX_ASSISTANT_ID",
    "TELNYX_CONNECTION_ID",
    "TELNYX_PHONE_NUMBER",
    "TELNYX_PUBLIC_KEY",
    "CALL_API_KEY",
    "PUBLIC_URL",
)


def run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=env,
        text=True,
        check=False,
    )


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def is_placeholder(value: str) -> bool:
    lowered = value.lower().strip()
    return (
        not lowered
        or lowered.startswith("your")
        or "example.com" in lowered
        or "xxxxxxxx" in lowered
        or lowered in {"replace_me", "todo", "<value>"}
    )


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def compose_command(action: list[str], use_tunnel: bool) -> list[str]:
    command = ["docker", "compose"]
    if use_tunnel:
        command.extend(["--profile", "tunnel"])
    return [*command, *action]


def main() -> int:
    if not ENV_FILE.exists():
        print("ERROR: .env is missing. Copy .env.example to .env and fill every required value.")
        return 1

    missing_private_files = [
        str(path.relative_to(PROJECT_DIR))
        for path in PRIVATE_CONFIG_FILES
        if not path.is_file()
    ]
    if missing_private_files:
        print("ERROR: Missing private configuration files:")
        for path in missing_private_files:
            print(f" - {path}")
        return 1

    env_values = parse_env(ENV_FILE)
    use_tunnel = bool(env_values.get("CLOUDFLARE_TUNNEL_TOKEN", "").strip())

    if "--down" in sys.argv:
        result = run(compose_command(["down", "--remove-orphans"], use_tunnel))
        return result.returncode

    if "--status" in sys.argv:
        result = run(compose_command(["ps"], use_tunnel))
        return result.returncode

    missing = [key for key in REQUIRED_ENV if is_placeholder(env_values.get(key, ""))]
    if missing:
        print("ERROR: Missing or placeholder values in .env:")
        for key in missing:
            print(f" - {key}")
        return 1

    requested_port = os.getenv("APP_PUBLIC_PORT") or env_values.get("APP_PUBLIC_PORT")
    public_port = int(requested_port) if requested_port else available_port()
    compose_env = os.environ.copy()
    compose_env["APP_PUBLIC_PORT"] = str(public_port)

    if run(compose_command(["config", "--quiet"], use_tunnel), compose_env).returncode != 0:
        return 1
    if run(compose_command(["build"], use_tunnel), compose_env).returncode != 0:
        return 1
    if run(
        compose_command(["up", "-d", "--no-build", "--remove-orphans", "--wait"], use_tunnel),
        compose_env,
    ).returncode != 0:
        return 1

    runtime = {
        "web": f"http://127.0.0.1:{public_port}",
        "health": f"http://127.0.0.1:{public_port}/health/ready",
        "public_url": env_values["PUBLIC_URL"],
        "tunnel_managed_by_compose": use_tunnel,
    }
    RUNTIME_FILE.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")

    print("Deployment is healthy.")
    print(f"Local endpoint: {runtime['web']}")
    print(f"Public endpoint: {runtime['public_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
