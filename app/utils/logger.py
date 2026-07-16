"""
Logging utility for the AI Voice Agent.

Provides structured logging with timestamps for tracking
call events, tool invocations, and errors.
"""

import logging
import sys
from datetime import datetime


def setup_logger(name: str = "voice_agent", level: int = logging.INFO) -> logging.Logger:
    """
    Set up and return a configured logger instance.

    Args:
        name: Logger name (default: "voice_agent")
        level: Logging level (default: INFO)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times
    if logger.handlers:
        return logger

    logger.setLevel(level)

    # Console handler with structured format
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-8s] [%(name)-15s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger


# Pre-configured loggers for different components
logger = setup_logger("voice_agent")
call_logger = setup_logger("voice_agent.call")
tool_logger = setup_logger("voice_agent.tool")
pipeline_logger = setup_logger("voice_agent.pipeline")


def log_call_started(call_sid: str, to_number: str) -> None:
    """Log when a call is initiated."""
    call_logger.info(
        f"📞 CALL STARTED | SID: {call_sid} | To: {to_number} | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_call_connected(call_sid: str) -> None:
    """Log when a call is connected and audio streaming begins."""
    call_logger.info(
        f"🔗 CALL CONNECTED | SID: {call_sid} | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_call_ended(call_sid: str, duration: float = 0.0) -> None:
    """Log when a call ends."""
    call_logger.info(
        f"📴 CALL ENDED | SID: {call_sid} | Duration: {duration:.1f}s | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_call_error(call_sid: str, error: str) -> None:
    """Log call-related errors."""
    call_logger.error(
        f"❌ CALL ERROR | SID: {call_sid} | Error: {error} | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_tool_invoked(tool_name: str, arguments: dict) -> None:
    """Log when a tool/function is invoked by the AI."""
    tool_logger.info(
        f"🔧 TOOL INVOKED | Tool: {tool_name} | Args: {arguments} | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_tool_result(tool_name: str, result: dict) -> None:
    """Log the result of a tool invocation."""
    tool_logger.info(
        f"✅ TOOL RESULT | Tool: {tool_name} | Result: {result} | "
        f"Time: {datetime.now().isoformat()}"
    )


def log_pipeline_event(event: str, details: str = "") -> None:
    """Log pipeline lifecycle events."""
    pipeline_logger.info(
        f"⚡ PIPELINE | Event: {event} | {details} | "
        f"Time: {datetime.now().isoformat()}"
    )
