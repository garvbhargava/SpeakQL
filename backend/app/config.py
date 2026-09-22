"""Configuration, validated at startup.

Backend Plan §18: config.py validates at startup and exits if a required
variable is missing. A backend that boots half-configured fails later, in a
request, where the cause is much harder to see.

One rule in here is load-bearing and easy to undo by accident:

    The bootstrap DSN is read from a variable this module does not define.

speakql_owner can create databases and roles. The API must never hold it. So
SPEAKQL_OWNER_DSN is deliberately absent from Settings -- an accidental
`from config import settings; settings.owner_dsn` fails at import, not at
runtime. scripts/bootstrap.sh reads it straight from the environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """A required variable is missing or malformed."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required and is not set")
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    # --- identity -----------------------------------------------------------
    env: str
    secret_key: str

    # --- the five runtime DSNs (§5.1). Note what is NOT here: the owner DSN --
    meta_dsn: str
    ro_dsn: str
    write_dsn: str
    edits_dsn: str
    uploads_dsn: str

    # --- models -------------------------------------------------------------
    llm_mode: str          # local | hosted
    llm_model: str
    llm_endpoint: str
    confidence_threshold: float

    # --- policy -------------------------------------------------------------
    free_email_mode: str   # personal_workspace | invite_only | blocked
    statement_timeout_ms: int
    max_rows: int
    invite_ttl_hours: int

    # --- rate limits (§7.3, §7.4) -------------------------------------------
    rate_ask_per_hour: int
    rate_upload_per_day: int
    rate_export_per_hour: int
    rate_codes_per_address_hour: int
    rate_codes_per_ip_hour: int

    warnings: tuple[str, ...] = field(default=(), compare=False)


_VALID_LLM_MODES = {"local", "hosted"}
_VALID_FREE_EMAIL_MODES = {"personal_workspace", "invite_only", "blocked"}


_FORBIDDEN_RUNTIME_USERS = {"speakql_owner", "postgres"}


def _user_of(dsn: str) -> str:
    """The username in a DSN, without importing SQLAlchemy at config time."""
    rest = dsn.split("://", 1)[-1]
    if "@" not in rest:
        return ""
    credentials = rest.rsplit("@", 1)[0]
    return credentials.split(":", 1)[0].lower()


def _refuse_privileged(name: str, dsn: str) -> None:
    """No runtime DSN may name the owner or the built-in superuser.

    The first version of this backend connected to its own database as
    speakql_owner -- a superuser -- while its documentation said the API never
    held the owner's credentials. Those two statements cannot both be true, and
    this check is what makes the second one enforced rather than asserted.
    """
    user = _user_of(dsn)
    if user in _FORBIDDEN_RUNTIME_USERS:
        raise ConfigError(
            f"{name} connects as {user!r}, which is a superuser. The API must "
            "run as a least-privilege role (speakql_app for META_DSN); the "
            "owner is for bootstrap.sh only."
        )


def load() -> Settings:
    """Build Settings from the environment, or raise ConfigError."""
    warnings: list[str] = []

    for name in ("META_DSN", "RO_DSN", "WRITE_DSN", "EDITS_DSN", "UPLOADS_DSN"):
        _refuse_privileged(name, os.environ.get(name, ""))

    llm_mode = _optional("LLM_MODE", "local")
    if llm_mode not in _VALID_LLM_MODES:
        raise ConfigError(
            f"LLM_MODE must be one of {sorted(_VALID_LLM_MODES)}, got {llm_mode!r}"
        )

    free_email_mode = _optional("FREE_EMAIL_MODE", "personal_workspace")
    if free_email_mode not in _VALID_FREE_EMAIL_MODES:
        raise ConfigError(
            f"FREE_EMAIL_MODE must be one of {sorted(_VALID_FREE_EMAIL_MODES)}, "
            f"got {free_email_mode!r}"
        )

    threshold = _float("CONFIDENCE_THRESHOLD", 0.55)
    if not 0.0 < threshold < 1.0:
        raise ConfigError(f"CONFIDENCE_THRESHOLD must sit between 0 and 1, got {threshold}")

    if os.environ.get("SPEAKQL_OWNER_DSN"):
        # Present in the environment is fine -- bootstrap.sh needs it. Loading
        # it into the API is not, and this module never does.
        warnings.append(
            "SPEAKQL_OWNER_DSN is set in this environment. It is used by "
            "bootstrap.sh only and is never loaded by the API."
        )

    return Settings(
        env=_optional("SPEAKQL_ENV", "development"),
        secret_key=_require("SECRET_KEY"),
        meta_dsn=_require("META_DSN"),
        ro_dsn=_require("RO_DSN"),
        write_dsn=_require("WRITE_DSN"),
        edits_dsn=_require("EDITS_DSN"),
        uploads_dsn=_require("UPLOADS_DSN"),
        llm_mode=llm_mode,
        llm_model=_optional("LLM_MODEL", "gemma3:4b"),
        llm_endpoint=_optional("LLM_ENDPOINT", "http://llm:11434"),
        confidence_threshold=threshold,
        free_email_mode=free_email_mode,
        statement_timeout_ms=_int("STATEMENT_TIMEOUT_MS", 10_000),
        max_rows=_int("MAX_ROWS", 5_000),
        invite_ttl_hours=_int("INVITE_TTL_HOURS", 168),
        rate_ask_per_hour=_int("RATE_ASK_PER_HOUR", 60),
        rate_upload_per_day=_int("RATE_UPLOAD_PER_DAY", 20),
        rate_export_per_hour=_int("RATE_EXPORT_PER_HOUR", 30),
        rate_codes_per_address_hour=_int("RATE_CODES_PER_ADDRESS_HOUR", 10),
        rate_codes_per_ip_hour=_int("RATE_CODES_PER_IP_HOUR", 30),
        warnings=tuple(warnings),
    )


def load_or_exit() -> Settings:
    """For main.py: report every problem plainly, then stop."""
    try:
        return load()
    except ConfigError as exc:
        print(f"speakql: configuration error: {exc}", file=sys.stderr)
        print("speakql: see .env.example for the full list", file=sys.stderr)
        raise SystemExit(2) from exc
