"""Security and validation utilities for the PSE&G integration."""

from __future__ import annotations

from datetime import datetime
import ipaddress
from typing import Any
import urllib.parse

from .const import CONF_AUTH_SERVER_URL, DEFAULT_AUTH_SERVER_URL

LOCAL_HOST_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def sanitize_url_for_logging(url: str | None) -> str:
    """Strip credentials (username:password) from a URL before logging."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        # If user credentials exist before '@', extract scheme and clean host/port
        if "@" in url:
            scheme = ""
            rest = url
            if "://" in url:
                scheme, _, rest = url.partition("://")
            elif url.startswith("http:/"):
                scheme = "http"
                rest = url[len("http:/"):].lstrip("/")
            elif url.startswith("https:/"):
                scheme = "https"
                rest = url[len("https:/"):].lstrip("/")

            base, sep_q, query = rest.partition("?")
            if "@" in base:
                _, _, host_part = base.rpartition("@")
                clean = f"{scheme}://{host_part}" if scheme else host_part
                return f"{clean}?{query}" if sep_q else clean

        parsed = urllib.parse.urlsplit(url)
        if parsed.username or parsed.password:
            hostname = parsed.hostname or ""
            port_part = f":{parsed.port}" if parsed.port else ""
            clean_netloc = f"{hostname}{port_part}"
            return parsed._replace(netloc=clean_netloc).geturl()
        return url
    except Exception:
        return "<invalid-url>"


def get_valid_auth_server_url(
    options: dict[str, Any] | None,
    data: dict[str, Any] | None,
    default: str | None = DEFAULT_AUTH_SERVER_URL,
) -> str | None:
    """Return the first valid auth server URL in priority order: options, data, default."""
    if options:
        opt_url = options.get(CONF_AUTH_SERVER_URL)
        if opt_url and is_valid_auth_server_url(opt_url):
            return str(opt_url).strip()
    if data:
        data_url = data.get(CONF_AUTH_SERVER_URL)
        if data_url and is_valid_auth_server_url(data_url):
            return str(data_url).strip()
    return default


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

        # Check IP addresses first (IPv4, IPv6, loopback, private RFC1918, Tailscale RFC6598)
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_loopback or ip.is_private or ip in TAILSCALE_NETWORK:
                return True
            return False
        except ValueError:
            pass

        # Disallow purely numeric or hex hostnames that could represent raw integer IPs
        if hostname.isdigit() or hostname.startswith("0x"):
            return False

        # Check local domain suffixes (.local, .lan, .home.arpa, .internal)
        if any(hostname.endswith(suffix) for suffix in LOCAL_HOST_SUFFIXES):
            return True

        # Single-label hostnames (e.g. nas, pseg_auth, server, localhost)
        if "." not in hostname and ":" not in hostname:
            return True

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
