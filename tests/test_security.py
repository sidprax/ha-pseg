"""Unit tests for security and validation utilities."""

from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Provide a proper base class for DataUpdateCoordinator so subclasses initialize real instances
class FakeDataUpdateCoordinator:
    def __init__(self, hass, logger, *, name=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    def __class_getitem__(cls, item):
        return cls


# Stub Home Assistant modules if not present in test environment
for mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.frontend",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.recorder",
    "homeassistant.components.recorder.statistics",
    "homeassistant.components.recorder.models",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.typing",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
]:
    if mod not in sys.modules:
        m = MagicMock()
        m.__path__ = []
        sys.modules[mod] = m

if "aiohttp" not in sys.modules:
    sys.modules["aiohttp"] = MagicMock()

# Inject FakeDataUpdateCoordinator onto the stubbed update_coordinator module
sys.modules["homeassistant.helpers.update_coordinator"].DataUpdateCoordinator = FakeDataUpdateCoordinator

from custom_components.pseg.util import is_valid_auth_server_url, is_valid_date_str
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator


class TestAuthServerUrlValidation(unittest.TestCase):
    """Test auth_server_url validation logic."""

    def test_valid_loopback_and_lan_http(self):
        self.assertTrue(is_valid_auth_server_url("http://127.0.0.1:8000"))
        self.assertTrue(is_valid_auth_server_url("http://localhost:8000"))
        self.assertTrue(is_valid_auth_server_url("http://pseg_auth:8000"))
        self.assertTrue(is_valid_auth_server_url("http://local-pseg-auth:8000"))
        self.assertTrue(is_valid_auth_server_url("http://homeassistant:8000"))
        self.assertTrue(is_valid_auth_server_url("http://homeassistant.local:8000"))
        self.assertTrue(is_valid_auth_server_url("http://192.168.1.150:8000"))
        self.assertTrue(is_valid_auth_server_url("http://10.0.0.2:8000"))
        self.assertTrue(is_valid_auth_server_url("http://172.16.0.5:8000"))

    def test_valid_remote_https(self):
        self.assertTrue(is_valid_auth_server_url("https://my-ha.duckdns.org:8000"))
        self.assertTrue(is_valid_auth_server_url("https://auth.internal.corp"))

    def test_insecure_remote_http_rejected(self):
        self.assertFalse(is_valid_auth_server_url("http://example.com:8000"))
        self.assertFalse(is_valid_auth_server_url("http://my-ha.duckdns.org:8000"))
        self.assertFalse(is_valid_auth_server_url("http://8.8.8.8:8000"))

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


class TestDateValidation(unittest.TestCase):
    """Test date format validation for service parameters."""

    def test_valid_iso_dates(self):
        self.assertTrue(is_valid_date_str("2026-10-30"))
        self.assertTrue(is_valid_date_str("2024-01-01"))
        self.assertTrue(is_valid_date_str("1999-12-31"))

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
    """Test coordinator re-authentication cooldown and lockout prevention."""

    def test_cooldown_and_max_failure_limits(self):
        hass = MagicMock()
        client = MagicMock()
        entry_data = {"username": "test@example.com", "password": "password123"}
        coord = PsegDataUpdateCoordinator(hass, client, entry_data)

        self.assertEqual(coord._reauth_failure_count, 0)
        self.assertIsNone(coord._last_reauth_attempt)

        # Failure count accumulation: when failure count reaches 3, automated re-auth must be blocked
        coord._reauth_failure_count = 3
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(coord.async_reauthenticate())
            self.assertFalse(result)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
