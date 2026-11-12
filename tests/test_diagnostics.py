"""Unit tests for the diagnostics download."""

import asyncio
from datetime import datetime, timezone
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# Ensure repo root is on sys.path for direct unittest invocation
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401  (stubs Home Assistant for direct unittest invocation)

REDACTED = "**REDACTED**"


def _redact(data, to_redact):
    """Same behavior as homeassistant.components.diagnostics.async_redact_data."""
    if not isinstance(data, (dict, list)):
        return data
    if isinstance(data, list):
        return [_redact(item, to_redact) for item in data]
    redacted = {**data}
    for key, value in redacted.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if key in to_redact:
            redacted[key] = REDACTED
        elif isinstance(value, dict):
            redacted[key] = _redact(value, to_redact)
        elif isinstance(value, list):
            redacted[key] = [_redact(item, to_redact) for item in value]
    return redacted


# Use the real helper when Home Assistant is installed, otherwise a faithful stand-in
_diag_mod = sys.modules.get("homeassistant.components.diagnostics")
if _diag_mod is None or isinstance(_diag_mod, MagicMock):
    _diag_mod = types.ModuleType("homeassistant.components.diagnostics")
    _diag_mod.REDACTED = REDACTED
    _diag_mod.async_redact_data = _redact
    sys.modules["homeassistant.components.diagnostics"] = _diag_mod

from custom_components.pseg.const import (  # noqa: E402
    AUTH_TYPE_COOKIE,
    AUTH_TYPE_MYMETER,
    CONF_AUTH_SERVER_URL,
    CONF_AUTH_TYPE,
    CONF_COOKIE,
    CONF_ELECTRIC_RATE,
    CONF_KEEP_ALIVE,
    CONF_MFA_METHOD,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.pseg.coordinator import PsegDataUpdateCoordinator  # noqa: E402
from custom_components.pseg.diagnostics import (  # noqa: E402
    async_get_config_entry_diagnostics,
)

USERNAME = "someone@example.com"
PASSWORD = "hunter2-secret"
COOKIE = "MM_SID=sid-secret-value; MM_RememberMe=remember-secret-value; .ASPXAUTH=aspx-secret"
SECRETS = [USERNAME, PASSWORD, "sid-secret-value", "remember-secret-value", "aspx-secret", "Main Meter 123456"]


def make_entry(data, options=None, title=None, unique_id=None):
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.version = 1
    entry.title = title if title is not None else "PSE&G Energy"
    entry.unique_id = unique_id
    entry.data = data
    entry.options = options or {}
    return entry


def make_hass(coordinators=None):
    hass = MagicMock()
    hass.data = {DOMAIN: coordinators} if coordinators is not None else {}
    return hass


def run(hass, entry):
    return asyncio.run(async_get_config_entry_diagnostics(hass, entry))


def addon_entry():
    return make_entry(
        {
            CONF_COOKIE: COOKIE,
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_AUTH_SERVER_URL: f"http://admin:{PASSWORD}@192.168.1.20:8000/login?token=abc",
            CONF_AUTH_TYPE: AUTH_TYPE_MYMETER,
            CONF_MFA_METHOD: "sms",
        },
        options={CONF_ELECTRIC_RATE: 0.2},
        title=f"PSE&G ({USERNAME})",
        unique_id=USERNAME.lower(),
    )


def loaded_coordinator(entry):
    client = MagicMock()
    client.cookie = COOKIE
    coordinator = PsegDataUpdateCoordinator(MagicMock(), client, dict(entry.data), config_entry=entry)
    coordinator.data = {
        "meter": "Main Meter 123456",
        "latest_date": "2026-10-31",
        "latest_kwh": 12.5,
        "history_14d": [{"date": "2026-10-31", "kwh": 12.5, "cost": 3.09, "is_exact_cost": False}],
        "hourly_records": [{"start": "x", "kwh": 0.5}] * 48,
    }
    coordinator.last_successful_update = "2026-11-01T12:00:00+00:00"
    coordinator.next_update_time = datetime(2026, 11, 1, 16, tzinfo=timezone.utc)
    return coordinator


class TestDiagnostics(unittest.TestCase):
    def assert_no_secrets(self, result):
        dumped = json.dumps(result, default=str)
        for secret in SECRETS:
            self.assertNotIn(secret, dumped)

    def test_addon_entry_redacts_credentials_and_keeps_debug_info(self):
        entry = addon_entry()
        result = run(make_hass({entry.entry_id: loaded_coordinator(entry)}), entry)

        self.assert_no_secrets(result)
        self.assertEqual(result["entry"]["title"], REDACTED)
        self.assertEqual(result["entry"]["unique_id"], REDACTED)
        data = result["entry"]["data"]
        self.assertEqual(data[CONF_COOKIE], REDACTED)
        self.assertEqual(data[CONF_PASSWORD], REDACTED)
        self.assertEqual(data[CONF_USERNAME], REDACTED)
        self.assertEqual(data[CONF_AUTH_SERVER_URL], "http://192.168.1.20:8000")
        self.assertEqual(data[CONF_AUTH_TYPE], AUTH_TYPE_MYMETER)
        self.assertEqual(result["entry"]["options"], {CONF_ELECTRIC_RATE: 0.2})
        self.assertFalse(result["keep_alive_enabled"])

        coord = result["coordinator"]
        self.assertEqual(coord["status"], "connected")
        self.assertFalse(coord["auth_required"])
        self.assertEqual(coord["next_update_time"], "2026-11-01T16:00:00+00:00")
        self.assertTrue(coord["has_cookie"])
        self.assertEqual(coord["cookie_names"], [".ASPXAUTH", "MM_RememberMe", "MM_SID"])
        self.assertEqual(coord["data"]["meter"], REDACTED)
        self.assertEqual(coord["data"]["latest_kwh"], 12.5)
        self.assertEqual(len(coord["data"]["history_14d"]), 1)
        self.assertNotIn("hourly_records", coord["data"])
        self.assertEqual(coord["data"]["hourly_record_count"], 48)

    def test_cookie_entry_before_first_update(self):
        entry = make_entry({CONF_COOKIE: COOKIE, CONF_AUTH_TYPE: AUTH_TYPE_COOKIE})
        result = run(make_hass(), entry)

        self.assert_no_secrets(result)
        self.assertEqual(result["entry"]["title"], "PSE&G Energy")
        self.assertIsNone(result["entry"]["unique_id"])
        self.assertTrue(result["keep_alive_enabled"])
        self.assertIsNone(result["coordinator"])

    def test_keep_alive_option_wins(self):
        entry = make_entry(
            {CONF_COOKIE: COOKIE, CONF_AUTH_TYPE: AUTH_TYPE_COOKIE}, options={CONF_KEEP_ALIVE: False}
        )
        self.assertFalse(run(make_hass(), entry)["keep_alive_enabled"])

    def test_auth_required_without_data(self):
        entry = addon_entry()
        coordinator = loaded_coordinator(entry)
        coordinator.data = None
        coordinator.client.cookie = ""
        coordinator.auth_required = True
        coordinator.status = "auth_required"
        coordinator.next_update_time = None
        result = run(make_hass({entry.entry_id: coordinator}), entry)

        self.assert_no_secrets(result)
        coord = result["coordinator"]
        self.assertEqual(coord["status"], "auth_required")
        self.assertTrue(coord["auth_required"])
        self.assertIsNone(coord["data"])
        self.assertIsNone(coord["next_update_time"])
        self.assertFalse(coord["has_cookie"])
        self.assertEqual(coord["cookie_names"], [])

    def test_discovered_meters_are_redacted(self):
        entry = addon_entry()
        coordinator = loaded_coordinator(entry)
        coordinator.discovered_services = [{
            "type_id": 4, "label": "Natural Gas", "selected": False,
            "meters": [{
                "meter_number": "000000000007", "internal_id": "1000008",
                "active": True, "rate_code": "(PGBRSGH - PGBRSGH)",
            }],
        }]
        result = run(make_hass({entry.entry_id: coordinator}), entry)
        meter = result["coordinator"]["services"][0]["meters"][0]
        self.assertEqual(meter["meter_number"], REDACTED)
        self.assertEqual(meter["internal_id"], REDACTED)
        self.assertTrue(meter["active"])
        self.assertEqual(meter["rate_code"], "(PGBRSGH - PGBRSGH)")
        self.assertNotIn("000000000007", json.dumps(result))
        self.assertNotIn("1000008", json.dumps(result))

    def test_auth_server_url_never_leaks_credentials(self):
        cases = {
            "http://127.0.0.1:8000": "http://127.0.0.1:8000",
            "http://homeassistant.local:8000/": "http://homeassistant.local:8000",
            f"http://user:pa?ss{PASSWORD}@10.0.0.5:8000": REDACTED,
            f"http://user:pa#ss{PASSWORD}@10.0.0.5:8000": REDACTED,
            f"https://user:{PASSWORD}@pseg.example.com/path?x=1": "https://pseg.example.com",
            f"http://user:12?{PASSWORD}@10.0.0.5:8000": REDACTED,
            f"http://user:12/{PASSWORD}@10.0.0.5:8000": REDACTED,
            "not a url": REDACTED,
            "http://[::1": REDACTED,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                entry = make_entry({CONF_COOKIE: COOKIE, CONF_AUTH_SERVER_URL: url})
                result = run(make_hass(), entry)
                self.assertEqual(result["entry"]["data"][CONF_AUTH_SERVER_URL], expected)
                dumped = json.dumps(result)
                self.assertNotIn(PASSWORD, dumped)
                self.assertNotIn("user", dumped)


if __name__ == "__main__":
    unittest.main()
