"""Outbound notifications."""

from __future__ import annotations

from .email import (
    EmailConfig,
    EmailConfigurationError,
    build_email_html,
    build_message,
    send_quarterly_email,
)

__all__ = [
    "EmailConfig",
    "EmailConfigurationError",
    "build_email_html",
    "build_message",
    "send_quarterly_email",
]
