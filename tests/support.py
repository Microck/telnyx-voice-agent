"""Shared deterministic environment for tests that import application settings."""

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


PRIVATE_KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY = PRIVATE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
)
TEST_DATABASE_PATH = Path("/tmp/voice-agent-tests.sqlite3")
TEST_DATABASE_PATH.unlink(missing_ok=True)

os.environ.update(
    {
        "TELNYX_API_KEY": "test-telnyx-key",
        "TELNYX_ASSISTANT_ID": "assistant-test",
        "TELNYX_CONNECTION_ID": "test-connection",
        "TELNYX_PHONE_NUMBER": "+15555550100",
        "TELNYX_PUBLIC_KEY": base64.b64encode(PUBLIC_KEY).decode("ascii"),
        "CALL_API_KEY": "test-call-api-key-32-characters-long",
        "NTFY_TOPIC": "test-call-notifications",
        "PUBLIC_URL": "https://voice.example.com",
        "CALL_DATABASE_PATH": str(TEST_DATABASE_PATH),
    }
)
