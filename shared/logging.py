"""Structured logging setup shared by every service.

RULES.md requires that every rejected packet is logged with a reason, and
that no secret, key, or credential is ever logged even at debug level. Two
things here enforce that second part:

* :func:`configure_logging` installs a processor that redacts any event key
  whose name looks secret-ish, so an accidental ``password=`` in a log call
  cannot leak.
* :func:`safe_fingerprint` gives a short, non-reversible way to refer to a
  signature or key in a log line instead of the value itself.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from typing import Any

import structlog

from shared.config import settings

#: Substrings that mark a field as secret. Matching keys are never emitted.
_REDACT_KEY_MARKERS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "private_key",
    "signing_key",
    "content_key",
    "encrypted_key",
    "pem",
    "authorization",
    "api_key",
)

REDACTED = "[redacted]"


def _redact_secrets(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Replace values whose key names suggest secret material."""
    for key in list(event_dict):
        lowered = key.lower()
        if any(marker in lowered for marker in _REDACT_KEY_MARKERS):
            event_dict[key] = REDACTED
    return event_dict


def safe_fingerprint(raw: bytes | str, length: int = 12) -> str:
    """A short one-way fingerprint, for referring to bytes without printing them.

    Used for signatures and key material in log lines. Truncated SHA-256, so
    it is correlatable across log entries but not reversible.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def configure_logging(*, service: str, json_output: bool = True) -> None:
    """Install the shared structlog configuration.

    Args:
        service: name bound to every log line from this process.
        json_output: JSON lines for machine consumption; set False for a
            human-readable console renderer during local debugging.
    """
    level = getattr(logging, settings.log_level, logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.bind_contextvars(service=service)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)
