"""
Configuration module for the AI Voice Agent.

Loads environment variables from .env file and provides a centralized
settings object for the entire application.
"""

import os
import sys
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Settings:
    """Application settings loaded from environment variables."""

    gemini_api_key: str
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_phone_number: str
    your_phone_number: str
    ngrok_url: str

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8000

    # External APIs
    weather_api_url: str = "https://api.open-meteo.com/v1"
    geocoding_api_url: str = "https://geocoding-api.open-meteo.com/v1"

    @property
    def websocket_url(self) -> str:
        """Generate the WebSocket URL for Twilio Media Streams."""
        # Convert https:// to wss://
        ws_base = self.ngrok_url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{ws_base}/ws"

    @property
    def twiml_url(self) -> str:
        """Generate the TwiML webhook URL."""
        return f"{self.ngrok_url}/twiml"


def _get_required_env(var_name: str) -> str:
    """Get a required environment variable or exit with an error."""
    value = os.getenv(var_name, "").strip()
    if not value or value.startswith("your_"):
        print(f"❌ ERROR: Environment variable '{var_name}' is not set or has a placeholder value.")
        print(f"   Please update your .env file with a valid value for {var_name}.")
        print(f"   See .env.example for guidance.")
        sys.exit(1)
    return value


def load_settings() -> Settings:
    """Load and validate all settings from environment variables."""
    return Settings(
        gemini_api_key=_get_required_env("GEMINI_API_KEY"),
        twilio_account_sid=_get_required_env("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=_get_required_env("TWILIO_AUTH_TOKEN"),
        twilio_phone_number=_get_required_env("TWILIO_PHONE_NUMBER"),
        your_phone_number=_get_required_env("YOUR_PHONE_NUMBER"),
        ngrok_url=_get_required_env("NGROK_URL"),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        weather_api_url=os.getenv("WEATHER_API_URL", "https://api.open-meteo.com/v1"),
        geocoding_api_url=os.getenv("GEOCODING_API_URL", "https://geocoding-api.open-meteo.com/v1"),
    )


# Singleton settings instance — loaded once on import
# Use try/except so imports don't fail during development/testing
try:
    settings = load_settings()
except SystemExit:
    # During development, allow importing without valid env vars
    # The app will fail at runtime with a clear error message
    settings = None
