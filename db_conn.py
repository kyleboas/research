"""Database connection helpers.

Railway deployments sometimes provide split PG* variables even when DATABASE_URL
is missing or misconfigured. This helper resolves a usable psycopg conninfo.
"""

from __future__ import annotations

import os
import re
from urllib.parse import parse_qs, urlparse


_URL_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_PRIVATE_URL",
    "DATABASE_PUBLIC_URL",
)


def _has_hostname(conninfo: str) -> bool:
    """Return True when conninfo clearly contains a hostname.

    Supports URL form (postgresql://...) and libpq key=value form.
    """
    if "://" in conninfo:
        parsed = urlparse(conninfo)
        if parsed.hostname:
            return True
        query = parse_qs(parsed.query)
        return bool(query.get("host") and query["host"][0].strip())

    # key=value style, e.g. "host=db.internal port=5432 ..."
    host_match = re.search(r"(?:^|\s)host\s*=\s*([^\s]+)", conninfo)
    return bool(host_match and host_match.group(1).strip())


def _build_from_pg_vars() -> str | None:
    host = os.environ.get("PGHOST", "").strip()
    port = os.environ.get("PGPORT", "").strip()
    user = os.environ.get("PGUSER", "").strip()
    password = os.environ.get("PGPASSWORD", "").strip()
    dbname = os.environ.get("PGDATABASE", "").strip()

    if not all([host, port, user, password, dbname]):
        return None

    return (
        f"host={host} port={port} user={user} "
        f"password={password} dbname={dbname}"
    )


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if value and "${{" in value and "}}" in value:
        return ""
    return value


def _first_valid_url() -> tuple[str | None, str | None]:
    """Return (value, reason) from URL-like env vars.

    reason can be:
    - None: valid value with hostname
    - missing_hostname: at least one URL-like var was set but had no host
    - None with value=None: no URL-like var was usable/set
    """
    saw_hostless = False
    for key in _URL_ENV_KEYS:
        raw = _clean_env_value(os.environ.get(key, ""))
        if not raw:
            continue
        if _has_hostname(raw):
            return raw, None
        saw_hostless = True

    if saw_hostless:
        return None, "missing_hostname"
    return None, None


def resolve_database_conninfo() -> tuple[str | None, str | None]:
    """Return (conninfo, reason_if_missing)."""
    raw, reason = _first_valid_url()
    if raw:
        return raw, None

    fallback = _build_from_pg_vars()
    if fallback:
        return fallback, None

    if reason == "missing_hostname":
        return None, "missing_hostname"
    return None, "missing_database_url"
