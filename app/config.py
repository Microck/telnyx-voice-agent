"""
Configuration module for the AI Voice Agent.

Loads environment variables from .env file and provides a centralized
settings object for the entire application.
"""

import base64
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment variables."""

    telnyx_api_key: str
    telnyx_assistant_id: str
    telnyx_connection_id: str
    telnyx_phone_number: str
    telnyx_public_key: str
    call_api_key: str
    ntfy_topic: str
    public_url: str
    your_phone_number: str = ""
    webhook_tolerance_seconds: int = 300
    call_database_path: Path = Path("/app/data/calls.sqlite3")
    recording_directory: Path = Path("/app/data/recordings")
    personal_knowledge_path: Path = Path("/app/config/personal-knowledge.md")
    sensitive_knowledge_path: Path = Path("/app/config/sensitive-knowledge.md")
    call_retention_days: int = 30
    spanish_voice: str = "Telnyx.Ultra.58e531e3-b212-49df-adee-c335a19c2429"
    english_voice: str = "Telnyx.KokoroTTS.am_michael"

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8000

    # External APIs
    weather_api_url: str = "https://api.open-meteo.com/v1"
    geocoding_api_url: str = "https://geocoding-api.open-meteo.com/v1"

    @property
    def webhook_url(self) -> str:
        """Generate the webhook URL for Telnyx call events."""
        return f"{self.public_url}/call-events"

    def derived_secret(self, purpose: str) -> str:
        """Keep externally exposed capabilities separate from the master key."""
        return hmac.new(
            self.call_api_key.encode("utf-8"),
            purpose.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @property
    def tool_api_key(self) -> str:
        return self.derived_secret("telnyx-assistant-tools:v1")

    @property
    def report_signing_key(self) -> str:
        return self.derived_secret("call-report-signing:v1")

def _get_required_env(var_name: str) -> str:
    """Get a required environment variable or raise a startup error."""
    value = os.getenv(var_name, "").strip()
    if not value or value.startswith("your_"):
        raise RuntimeError(
            f"Environment variable '{var_name}' is not set or has a placeholder value. "
            "See .env.example for guidance."
        )
    return value


def _get_secret_env(var_name: str) -> str:
    value = _get_required_env(var_name)
    if len(value) < 32:
        raise RuntimeError(f"Environment variable '{var_name}' must contain at least 32 characters.")
    return value


def _get_public_url() -> str:
    value = _get_required_env("PUBLIC_URL").rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path:
        raise RuntimeError("PUBLIC_URL must be an HTTPS origin without a path or trailing slash.")
    return value


def _get_ntfy_topic() -> str:
    value = _get_required_env("NTFY_TOPIC")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", value):
        raise RuntimeError(
            "NTFY_TOPIC must contain 8-64 ASCII letters, numbers, hyphens, or underscores."
        )
    return value


def _get_telnyx_public_key() -> str:
    value = _get_required_env("TELNYX_PUBLIC_KEY")
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise RuntimeError("TELNYX_PUBLIC_KEY must be valid Base64.") from error
    if len(decoded) != 32:
        raise RuntimeError("TELNYX_PUBLIC_KEY must decode to a 32-byte Ed25519 public key.")
    return value


def _get_positive_int(var_name: str, default: str) -> int:
    try:
        value = int(os.getenv(var_name, default))
    except ValueError as error:
        raise RuntimeError(f"Environment variable '{var_name}' must be an integer.") from error
    if value <= 0:
        raise RuntimeError(f"Environment variable '{var_name}' must be greater than zero.")
    return value


def load_settings() -> Settings:
    """Load and validate all settings from environment variables."""
    return Settings(
        telnyx_api_key=_get_required_env("TELNYX_API_KEY"),
        telnyx_assistant_id=_get_required_env("TELNYX_ASSISTANT_ID"),
        telnyx_connection_id=_get_required_env("TELNYX_CONNECTION_ID"),
        telnyx_phone_number=_get_required_env("TELNYX_PHONE_NUMBER"),
        telnyx_public_key=_get_telnyx_public_key(),
        call_api_key=_get_secret_env("CALL_API_KEY"),
        ntfy_topic=_get_ntfy_topic(),
        your_phone_number=os.getenv("YOUR_PHONE_NUMBER", ""),
        public_url=_get_public_url(),
        webhook_tolerance_seconds=_get_positive_int("WEBHOOK_TOLERANCE_SECONDS", "300"),
        call_database_path=Path(os.getenv("CALL_DATABASE_PATH", "/app/data/calls.sqlite3")),
        recording_directory=Path(os.getenv("RECORDING_DIRECTORY", "/app/data/recordings")),
        personal_knowledge_path=Path(
            os.getenv("PERSONAL_KNOWLEDGE_PATH", "/app/config/personal-knowledge.md")
        ),
        sensitive_knowledge_path=Path(
            os.getenv("SENSITIVE_KNOWLEDGE_PATH", "/app/config/sensitive-knowledge.md")
        ),
        call_retention_days=_get_positive_int("CALL_RETENTION_DAYS", "30"),
        spanish_voice=os.getenv(
            "TELNYX_SPANISH_VOICE",
            "Telnyx.Ultra.58e531e3-b212-49df-adee-c335a19c2429",
        ).strip(),
        english_voice=os.getenv(
            "TELNYX_ENGLISH_VOICE",
            "Telnyx.KokoroTTS.am_michael",
        ).strip(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=_get_positive_int("PORT", "8000"),
        weather_api_url=os.getenv("WEATHER_API_URL", "https://api.open-meteo.com/v1"),
        geocoding_api_url=os.getenv("GEOCODING_API_URL", "https://geocoding-api.open-meteo.com/v1"),
    )


# Fail during startup instead of serving a misleading health response with an
# unusable configuration.
settings = load_settings()
