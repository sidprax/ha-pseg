"""Diagnostics support for PSE&G Energy.

Lets users download a debug file from the integration page to attach to an issue.
Cookies, passwords, MFA codes, usernames and the meter name are never included.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_AUTH_SERVER_URL,
    CONF_COOKIE,
    CONF_MFA_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from .coordinator import keep_alive_enabled

TO_REDACT = {
    CONF_COOKIE,
    CONF_PASSWORD,
    CONF_MFA_CODE,
    CONF_USERNAME,
    # Poll data: the meter name from the portal is account data
    "meter",
}


def _auth_server_host(url: Any) -> Any:
    """Keep only scheme, host and port of the add-on URL, never credentials or a path."""
    if not url or not isinstance(url, str):
        return url
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        port = parts.port
    except ValueError:
        return REDACTED
    if not parts.scheme or not host:
        return REDACTED
    # An unencoded "?", "#" or "/" in a password ends the host part early, so the
    # parsed host would be the username or part of the password
    if "@" in url and "@" not in parts.netloc:
        return REDACTED
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


def _cookie_names(cookie: Any) -> list[str]:
    """Names of the stored cookies, so a missing session cookie shows. Never values."""
    if not cookie or not isinstance(cookie, str):
        return []
    names = set()
    for part in cookie.split(";"):
        name, sep, _ = part.partition("=")
        name = name.strip()
        if sep and name:
            names.add(name)
    return sorted(names)


def _redact_title(title: str, entry: ConfigEntry) -> str:
    """Entry titles look like "PSE&G (<username>)"."""
    username = entry.data.get(CONF_USERNAME)
    if title and username and str(username).lower() in title.lower():
        return REDACTED
    return title


def _summarize_data(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Poll data with the meter name redacted and the hourly series reduced to a count."""
    if data is None:
        return None
    summary = async_redact_data(dict(data), TO_REDACT)
    hourly = summary.pop("hourly_records", None)
    summary["hourly_record_count"] = len(hourly) if isinstance(hourly, list) else None
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = async_redact_data(dict(entry.data), TO_REDACT)
    if CONF_AUTH_SERVER_URL in data:
        data[CONF_AUTH_SERVER_URL] = _auth_server_host(data[CONF_AUTH_SERVER_URL])

    diagnostics: dict[str, Any] = {
        "entry": {
            "title": _redact_title(entry.title, entry),
            # The unique_id is the username for entries created with a sign-in
            "unique_id": REDACTED if entry.unique_id else None,
            "version": entry.version,
            "data": data,
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "keep_alive_enabled": keep_alive_enabled(entry.data, entry.options),
        "coordinator": None,
    }

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        return diagnostics

    next_update = coordinator.next_update_time
    diagnostics["coordinator"] = {
        "status": coordinator.status,
        "auth_required": coordinator.auth_required,
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_successful_update": coordinator.last_successful_update,
        "next_update_time": next_update.isoformat() if next_update else None,
        "effective_rate": coordinator.effective_rate,
        "rate_source": coordinator.rate_source,
        "days_used_for_rate": coordinator.days_used_for_rate,
        "has_cookie": bool(coordinator.client.cookie),
        "cookie_names": _cookie_names(coordinator.client.cookie),
        "data": _summarize_data(coordinator.data),
    }
    return diagnostics
