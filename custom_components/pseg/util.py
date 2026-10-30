"""Security and validation utilities for the PSE&G integration."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "pseg_auth", "local-pseg-auth", "homeassistant"}
DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_auth_server_url(url: str | None) -> bool:
    """Validate that auth_server_url is a safe http(s) endpoint.

    Ensures:
    - Scheme is strictly http or https.
    - If http, host must be loopback, private LAN (RFC1918), or local container name.
    - If remote host, scheme must be https.
    """
    if not url or not isinstance(url, str):
        return False

    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            return False

        if scheme == "https":
            return True

        if hostname in LOCAL_HOSTS or hostname.endswith(".local"):
            return True

        try:
            ip = ipaddress.ip_address(hostname)
            return ip.is_loopback or ip.is_private
        except ValueError:
            return False
    except Exception:
        return False


def is_valid_date_str(date_str: str | None) -> bool:
    """Check if a date string is formatted strictly as YYYY-MM-DD."""
    if not date_str or not isinstance(date_str, str):
        return False
    return bool(DATE_REGEX.match(date_str.strip()))
