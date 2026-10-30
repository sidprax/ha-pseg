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
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.pseg import _async_handle_backfill_service
from custom_components.pseg.const import (
    CONF_AUTH_SERVER_URL,
    DEFAULT_AUTH_SERVER_URL,
    DOMAIN,
)
from custom_components.pseg.util import (
    get_valid_auth_server_url,
    is_valid_auth_server_url,
    is_valid_date_str,
    sanitize_url_for_logging,
)
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator, ReauthStatus
from custom_components.pseg.pseg_client import PsegAuthError


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
        self.assertTrue(is_valid_auth_server_url("http://[::1]:8000"))
        self.assertTrue(is_valid_auth_server_url("http://[fe80::1]:8000"))

    def test_valid_tailscale_addresses(self):
        self.assertTrue(is_valid_auth_server_url("http://100.64.0.1:8000"))
        self.assertTrue(is_valid_auth_server_url("http://100.100.50.25:8080"))
        self.assertTrue(is_valid_auth_server_url("http://100.127.255.254:8000"))

    def test_valid_remote_https(self):
        self.assertTrue(is_valid_auth_server_url("https://my-ha.duckdns.org:8000"))
        self.assertTrue(is_valid_auth_server_url("https://auth.internal.corp"))
        self.assertTrue(is_valid_auth_server_url("https://example.com/pseg"))
        self.assertTrue(is_valid_auth_server_url("https://[2001:4860:4860::8888]:8000"))

    def test_insecure_remote_http_rejected(self):
        self.assertFalse(is_valid_auth_server_url("http://example.com:8000"))
        self.assertFalse(is_valid_auth_server_url("http://my-ha.duckdns.org:8000"))
        self.assertFalse(is_valid_auth_server_url("http://8.8.8.8:8000"))
        # Public IPv6 literals over HTTP must be rejected
        self.assertFalse(is_valid_auth_server_url("http://[2001:4860:4860::8888]:8000"))
        # Numeric / hex IPv4 forms over HTTP must be rejected
        self.assertFalse(is_valid_auth_server_url("http://134744072:8000"))
        self.assertFalse(is_valid_auth_server_url("http://0x08080808:8000"))
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


class TestGetValidAuthServerUrl(unittest.TestCase):
    """Test get_valid_auth_server_url priority and fallback logic."""

    def test_options_preferred_when_valid(self):
        options = {CONF_AUTH_SERVER_URL: "http://nas:8000"}
        data = {CONF_AUTH_SERVER_URL: "http://192.168.1.100:8000"}
        res = get_valid_auth_server_url(options, data)
        self.assertEqual(res, "http://nas:8000")

    def test_options_invalid_falls_back_to_valid_data(self):
        # Invalid options URL must NOT clobber valid data URL
        options = {CONF_AUTH_SERVER_URL: "http://public-evil.com:8000"}
        data = {CONF_AUTH_SERVER_URL: "http://nas:8000"}
        res = get_valid_auth_server_url(options, data)
        self.assertEqual(res, "http://nas:8000")

    def test_both_invalid_falls_back_to_default(self):
        options = {CONF_AUTH_SERVER_URL: "http://bad.example.com"}
        data = {CONF_AUTH_SERVER_URL: "http://bad.example.org"}
        res = get_valid_auth_server_url(options, data, default=DEFAULT_AUTH_SERVER_URL)
        self.assertEqual(res, DEFAULT_AUTH_SERVER_URL)

    def test_options_none_reads_valid_data(self):
        data = {CONF_AUTH_SERVER_URL: "http://100.64.0.1:8000"}
        res = get_valid_auth_server_url(None, data)
        self.assertEqual(res, "http://100.64.0.1:8000")

    def test_both_none_or_missing_returns_default(self):
        self.assertEqual(get_valid_auth_server_url(None, None), DEFAULT_AUTH_SERVER_URL)
        self.assertEqual(get_valid_auth_server_url({}, {}), DEFAULT_AUTH_SERVER_URL)


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

    def test_malformed_and_edge_case_credentials(self):
        # Specific reviewer edge cases
        self.assertEqual(sanitize_url_for_logging("admin:s3cret@nas:8000"), "nas:8000")
        self.assertEqual(sanitize_url_for_logging("http:/admin:s3cret@nas:8000"), "http://nas:8000")
        self.assertEqual(sanitize_url_for_logging("http://admin:pa/ss@nas:8000"), "http://nas:8000")
        self.assertEqual(sanitize_url_for_logging("http://admin:secret@nas:8000"), "http://nas:8000")


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

    def test_service_strips_whitespace_dates(self):
        loop = asyncio.new_event_loop()
        try:
            fake_hass = MagicMock()
            fake_coord = MagicMock()
            fake_coord.async_backfill_history = AsyncMock()
            fake_hass.data = {DOMAIN: {"test_entry": fake_coord}}

            # Service call with whitespace-padded dates
            call_valid = MagicMock()
            call_valid.data = {"start_date": "  2026-10-01  ", "end_date": " 2026-10-02 "}
            loop.run_until_complete(_async_handle_backfill_service(fake_hass, call_valid))
            fake_coord.async_backfill_history.assert_called_once_with("2026-10-01", "2026-10-02")

            # Service call with invalid date raises HomeAssistantError
            call_invalid = MagicMock()
            call_invalid.data = {"start_date": "  2026-13-45  "}
            with self.assertRaises(HomeAssistantError):
                loop.run_until_complete(_async_handle_backfill_service(fake_hass, call_invalid))
        finally:
            loop.close()


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
            self.assertEqual(result, "skipped_cooldown")
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
            self.assertEqual(result, "skipped_max_failures")
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
            # Verify allow_redirects=False is strictly passed to session.post
            call_kwargs = self.session.post.call_args[1]
            self.assertIs(call_kwargs.get("allow_redirects"), False)
        finally:
            loop.close()

    def test_companion_5xx_does_not_increment_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                return_value=MockResponse(500, {"error": "Server error in webdriver"})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 0)
            self.assertTrue(self.coord._addon_connected)
        finally:
            loop.close()

    def test_portal_403_increments_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                return_value=MockResponse(403, {"error": "recaptcha_blocked"})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 1)
            self.assertTrue(self.coord._addon_connected)
        finally:
            loop.close()

    def test_redirect_3xx_does_not_increment_failures(self):
        loop = asyncio.new_event_loop()
        try:
            self.session.post = MagicMock(
                return_value=MockResponse(302, {"location": "http://127.0.0.1:8000/login"})
            )
            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertEqual(self.coord._reauth_failure_count, 0)
            self.assertTrue(self.coord._addon_connected)
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

    def test_asyncio_gather_concurrency_lock(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord.auth_required = True
            post_call_count = 0

            async def delayed_post(*args, **kwargs):
                nonlocal post_call_count
                post_call_count += 1
                await asyncio.sleep(0.02)
                return MockResponse(200, {"success": True, "cookies": "MM_SID=fresh"})

            self.session.post = MagicMock(side_effect=delayed_post)

            async def run_gather():
                return await asyncio.gather(
                    self.coord.async_reauthenticate(),
                    self.coord.async_reauthenticate(),
                )

            res1, res2 = loop.run_until_complete(run_gather())
            self.assertTrue(res1)
            self.assertTrue(res2)
            # Both concurrent requests succeed, but session.post is only executed once
            self.assertEqual(post_call_count, 1)
        finally:
            loop.close()

    def test_cooldown_stamped_at_attempt_completion(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord.auth_required = True
            attempt_completion_time = None

            async def slow_login(*args, **kwargs):
                nonlocal attempt_completion_time
                await asyncio.sleep(0.02)
                attempt_completion_time = datetime.now(timezone.utc)
                return MockResponse(401, {"error": "Invalid password"})

            self.session.post = MagicMock(side_effect=slow_login)

            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertFalse(result)
            self.assertIsNotNone(self.coord._last_reauth_attempt)
            self.assertGreaterEqual(self.coord._last_reauth_attempt, attempt_completion_time)

            # An immediate subsequent attempt must hit cooldown without calling post again
            res_subsequent = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertEqual(res_subsequent, "skipped_cooldown")
            self.assertEqual(self.session.post.call_count, 1)
        finally:
            loop.close()

    def test_cooldown_skip_raises_update_failed_in_async_update_data(self):
        loop = asyncio.new_event_loop()
        try:
            now = datetime.now(timezone.utc)
            self.coord._last_reauth_attempt = now - timedelta(seconds=10)
            self.client.fetch_energy_data = MagicMock(
                side_effect=PsegAuthError("Session expired")
            )
            self.session.post = MagicMock()

            # Calling _async_update_data during cooldown must raise UpdateFailed, not ConfigEntryAuthFailed
            with self.assertRaises(UpdateFailed):
                loop.run_until_complete(self.coord._async_update_data())

            self.session.post.assert_not_called()
        finally:
            loop.close()

    def test_normal_successful_update_resets_reauth_failure_count(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord._reauth_failure_count = 2
            self.client.fetch_energy_data = MagicMock(
                return_value={"hourly_records": [], "effective_rate": 0.20}
            )

            loop.run_until_complete(self.coord._async_update_data())
            self.assertEqual(self.coord._reauth_failure_count, 0)
        finally:
            loop.close()

    def test_max_failures_preserves_addon_connected_status(self):
        loop = asyncio.new_event_loop()
        try:
            self.coord._addon_connected = True
            self.coord._reauth_failure_count = 3
            self.session.post = MagicMock()

            result = loop.run_until_complete(self.coord.async_reauthenticate())
            self.assertEqual(result, "skipped_max_failures")
            self.session.post.assert_not_called()
            # _addon_connected must remain True so user gets auth failure, not companion offline
            self.assertTrue(self.coord._addon_connected)
        finally:
            loop.close()

    def test_backfill_sets_auth_required_and_triggers_reauth(self):
        loop = asyncio.new_event_loop()
        try:
            # Simulate initial state after successful poll (auth_required is False)
            self.coord.auth_required = False
            self.coord.status = "connected"

            async def fake_executor(func, *args):
                return func(*args)

            self.hass.async_add_executor_job = fake_executor
            self.session.post = MagicMock(
                return_value=MockResponse(200, {"success": True, "cookies": "MM_SID=fresh"})
            )

            # fetch_history_range raises PsegAuthError on first try, succeeds on second
            fetch_mock = MagicMock(
                side_effect=[
                    PsegAuthError("Session expired during history request"),
                    [{"start": 1700000000, "kwh": 5.0, "cost": 1.0, "type": "electric"}],
                ]
            )
            self.client.fetch_history_range = fetch_mock

            # Run backfill
            result = loop.run_until_complete(
                self.coord.async_backfill_history("2026-10-01", "2026-10-02")
            )
            self.assertEqual(result, 1)
            # Verify session.post was called because backfill set auth_required=True
            self.session.post.assert_called_once()
            self.assertFalse(self.coord.auth_required)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
