"""Security and validation utilities for the PSE&G integration."""

from __future__ import annotations

from datetime import datetime
import ipaddress
import urllib.parse

LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def sanitize_url_for_logging(url: str | None) -> str:
    """Strip credentials (username:password) from a URL before logging."""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.username or parsed.password:
            hostname = parsed.hostname or ""
            port_part = f":{parsed.port}" if parsed.port else ""
            clean_netloc = f"{hostname}{port_part}"
            return parsed._replace(netloc=clean_netloc).geturl()
        return url.strip()
    except Exception:
        return "<invalid-url>"


def is_valid_auth_server_url(url: str | None) -> bool:
    """Validate that auth_server_url is a safe http(s) endpoint.

    Ensures:
    - Scheme is strictly http or https.
    - If http, host must be loopback, private LAN (RFC1918), Tailscale (RFC6598),
      single-label local/docker hostname, or local domain suffix.
    - If remote public host, scheme must be https.
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

        # Any valid https endpoint is accepted
        if scheme == "https":
            return True

        # Check local domain suffixes (.local, .lan, .home.arpa, .internal)
        if any(hostname.endswith(suffix) for suffix in LOCAL_HOST_SUFFIXES):
            return True

        # Single-label hostnames (e.g. nas, pseg_auth, server, localhost)
        if "." not in hostname:
            return True

        # Check IP addresses (loopback, RFC1918 private, or Tailscale 100.64.0.0/10)
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_loopback or ip.is_private or ip in TAILSCALE_NETWORK:
                return True
            return False
        except ValueError:
            return False
    except Exception:
        return False


def is_valid_date_str(date_str: str | None) -> bool:
    """Check if a date string is a real calendar date formatted strictly as YYYY-MM-DD."""
    if not date_str or not isinstance(date_str, str):
        return False
    date_str = date_str.strip()
    if len(date_str) != 10 or date_str[4] != "-" or date_str[7] != "-":
        return False
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False
