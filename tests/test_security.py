"""Unit tests for security, sanitization, and validation utilities."""

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import tests.conftest  # noqa: F401

import aiohttp
from custom_components.pseg.util import (
    is_valid_auth_server_url,
    is_valid_date_str,
    sanitize_url_for_logging,
)
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator


class MockResponse:
    """Mock aiohttp ClientResponse async context manager."""

    def __init__(self, status=200, json_data=None):
        self.status = status
        self._json_data = json_data or {}

    async def json(self):
        return self._json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


class TestAuthServerUrlValidation(unittest.TestCase):
    """Test auth_server_url validation logic."""

    def test_valid_loopback_and_lan_http(self):
        self.assertTrue(is_valid_auth_server_url("http://127.0.0.1:8000"))
        self.assertTrue(is_valid_auth_server_url("http://localhost:8000"))
        self.assertTrue(is_valid_auth_server_url("http://pseg_auth:8000"))
        self.assertTrue(is_valid_auth_server_url("http://local-pseg-auth:8000"))
        self.assertTrue(is_valid_auth_server_url("http://homeassistant:8000"))
        self.assertTrue(is_valid_auth_server_url("http://nas:8000"))
        self.assertTrue(is_valid_auth_server_url("http://auth-server:8080"))
        self.assertTrue(is_valid_auth_server_url("http://homeassistant.local:8000"))
        self.assertTrue(is_valid_auth_server_url("http://pseg.lan:8000"))
        self.assertTrue(is_valid_auth_server_url("http://ha.home.arpa:8000"))
        self.assertTrue(is_valid_auth_server_url("http://nas.internal:8000"))
        self.assertTrue(is_valid_auth_server_url("http://192.168.1.150:8000"))
        self.assertTrue(is_valid_auth_server_url("http://10.0.0.2:8000"))
        self.assertTrue(is_valid_auth_server_url("http://172.16.0.5:8000"))

    def test_valid_tailscale_addresses(self):
        self.assertTrue(is_valid_auth_server_url("http://100.64.0.1:8000"))
        self.assertTrue(is_valid_auth_server_url("http://100.100.50.25:8080"))
        self.assertTrue(is_valid_auth_server_url("http://100.127.255.254:8000"))

    def test_valid_remote_https(self):
        self.assertTrue(is_valid_auth_server_url("https://my-ha.duckdns.org:8000"))
        self.assertTrue(is_valid_auth_server_url("https://auth.internal.corp"))
        self.assertTrue(is_valid_auth_server_url("https://example.com/pseg"))

    def test_insecure_remote_http_rejected(self):
        self.assertFalse(is_valid_auth_server_url("http://example.com:8000"))
        self.assertFalse(is_valid_auth_server_url("http://my-ha.duckdns.org:8000"))
        self.assertFalse(is_valid_auth_server_url("http://8.8.8.8:8000"))
        # 100.128.0.1 is outside the Tailscale 100.64.0.0/10 prefix
        self.assertFalse(is_valid_auth_server_url("http://100.128.0.1:8000"))

    def test_unsupported_schemes_rejected(self):
        self.assertFalse(is_valid_auth_server_url("file:///etc/passwd"))
        self.assertFalse(is_valid_auth_server_url("ftp://127.0.0.1:8000"))
        self.assertFalse(is_valid_auth_server_url("gopher://127.0.0.1"))
        self.assertFalse(is_valid_auth_server_url("javascript:alert(1)"))

    def test_malformed_and_empty_rejected(self):
        self.assertFalse(is_valid_auth_server_url(None))
        self.assertFalse(is_valid_auth_server_url(""))
        self.assertFalse(is_valid_auth_server_url("   "))
        self.assertFalse(is_valid_auth_server_url("http://"))
        self.assertFalse(is_valid_auth_server_url(12345))


class TestSanitizeUrlForLogging(unittest.TestCase):
    """Test credential stripping from URLs before logging."""

    def test_basic_auth_stripped(self):
        url = "http://admin:secret123@127.0.0.1:8000/login"
        self.assertEqual(sanitize_url_for_logging(url), "http://127.0.0.1:8000/login")

        url_ssl = "https://user:pass@remote.domain.com:8443/auth"
        self.assertEqual(sanitize_url_for_logging(url_ssl), "https://remote.domain.com:8443/auth")

        url_user_only = "http://username@nas:8000/"
        self.assertEqual(sanitize_url_for_logging(url_user_only), "http://nas:8000/")

    def test_clean_urls_unchanged(self):
        url = "http://127.0.0.1:8000"
        self.assertEqual(sanitize_url_for_logging(url), "http://127.0.0.1:8000")

        url_https = "https://example.com/api"
        self.assertEqual(sanitize_url_for_logging(url_https), "https://example.com/api")

    def test_empty_and_invalid_urls(self):
        self.assertEqual(sanitize_url_for_logging(None), "")
        self.assertEqual(sanitize_url_for_logging(""), "")


class TestDateValidation(unittest.TestCase):
    """Test date format validation for service parameters."""

    def test_valid_iso_dates(self):
        self.assertTrue(is_valid_date_str("2026-10-30"))
        self.assertTrue(is_valid_date_str("2024-01-01"))
        self.assertTrue(is_valid_date_str("2024-02-29"))  # 2024 is a leap year
        self.assertTrue(is_valid_date_str("1999-12-31"))

    def test_impossible_dates_rejected(self):
        # Strict calendar validation catches invalid months and days
        self.assertFalse(is_valid_date_str("2026-13-45"))
        self.assertFalse(is_valid_date_str("2026-02-29"))  # 2026 is not a leap year
        self.assertFalse(is_valid_date_str("2026-04-31"))  # April has 30 days
        self.assertFalse(is_valid_date_str("2026-00-10"))

    def test_invalid_date_formats(self):
        self.assertFalse(is_valid_date_str("10/30/2026"))
        self.assertFalse(is_valid_date_str("2026-1-1"))
        self.assertFalse(is_valid_date_str("2026-10-30T12:00:00Z"))
        self.assertFalse(is_valid_date_str("yesterday"))
        self.assertFalse(is_valid_date_str("20261030"))

    def test_empty_and_non_string_dates(self):
        self.assertFalse(is_valid_date_str(None))
        self.assertFalse(is_valid_date_str(""))
        self.assertFalse(is_valid_date_str("   "))
        self.assertFalse(is_valid_date_str(20261030))


class TestCoordinatorReauthSafeguards(unittest.TestCase):
    """Test coordinator re-authentication cooldown, lockout prevention, and error counting."""

    def setUp(self):
        self.hass = MagicMock()
        self.client = MagicMock()
        self.entry_data = {
            "username": "test@example.com",
            "password": "password123",
            "auth_server_url": "http://127.0.0.1:8000",
        }
        self.coord = PsegDataUpdateCoordinator(self.hass, self.client, self.entry_data)
        self.coord.auth_required = True
        self.session = MagicMock()
        sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession.return_value = self.session

    def test_cooldown_blocks_rapid_retry(self):
        loop = asyncio.new_event_loop()
        try:
            now = datetime.now(timezone.utc)
            self.coord._last_reauth_attempt = now - timedelta(seconds=15)
            self.session.post = MagicMock()

            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.session.post.assert_not_called()
        finally:
            loop.close()

    def test_max_failure_limit_blocks_attempt(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord._reauth_failure_count = 3
            self.session.post = MagicMock()

            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.session.post.assert_not_called()
        finally:
            loop.close()

    def test_unreachable_companion_does_not_increment_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError("Connection refused"))
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 0)
            self.assertFalse(self.coord._addon_connected)
        finally:
            loop.close()

    def test_mfa_required_does_not_increment_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                return_value=MockResponse(200, {"success": False, "mfa_required": True})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 0)
            self.assertTrue(self.coord._addon_connected)
        finally:
            loop.close()

    def test_portal_rejection_increments_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                return_value=MockResponse(200, {"success": False, "error": "Invalid password"})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 1)
            self.assertTrue(self.coord._addon_connected)
        finally:
            loop.close()

    def test_successful_reauth_resets_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord._reauth_failure_count = 2
            self.session.post = MagicMock(
                return_value=MockResponse(200, {"success": True, "cookies": "MM_SID=xyz123"})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertTrue(result)
            self.assertEqual(self.coord._reauth_failure_count, 0)
            self.assertFalse(self.coord.auth_required)
            # Verify allow_redirects=False is passed to session.post
            call_kwargs = self.session.post.call_args[1]
            self.assertFalse(call_kwargs.get("allow_redirects"))
        finally:
            loop.close()

    def test_concurrent_task_lock_bypasses_redundant_login(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord.auth_required = False
            self.session.post = MagicMock()
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertTrue(result)
            self.session.post.assert_not_called()
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
