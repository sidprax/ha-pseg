"""Unit tests for the MyMeter session keep-alive."""

from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import requests

# Ensure repo root is on sys.path for direct unittest invocation
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401  (stubs Home Assistant for direct unittest invocation)

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.pseg.const import (
    AUTH_TYPE_COOKIE,
    AUTH_TYPE_MYMETER,
    AUTH_TYPE_OKTA,
    CONF_AUTH_TYPE,
    CONF_COOKIE,
    CONF_KEEP_ALIVE,
    CONF_PASSWORD,
    CONF_USERNAME,
    KEEPALIVE_MAX_SECONDS,
    KEEPALIVE_MIN_SECONDS,
    KEEPALIVE_RETRY_SECONDS,
)
from custom_components.pseg.coordinator import (
    PsegDataUpdateCoordinator,
    keep_alive_enabled,
    next_keep_alive_delay,
)
from custom_components.pseg.pseg_client import (
    BASE_URL,
    KEEPALIVE_PATH,
    PsegAuthError,
    PsegClient,
    PsegConnectionError,
    is_login_url,
)

HOST = "mysmartenergy.nj.pseg.com"


def response(
    status: int, url: str = f"{BASE_URL}/Dashboard", location: str = "", json_body=None
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.url = url
    resp.is_redirect = 300 <= status < 400 and bool(location)
    resp.headers = {"Location": location} if location else {}
    if json_body is None:
        resp.json.side_effect = ValueError("not JSON")
    else:
        resp.json.return_value = json_body
    return resp


class TestKeepAliveEnabled(unittest.TestCase):
    """The toggle defaults to on for pasted cookies and off for add-on sign-ins."""

    def test_default_on_for_cookie(self):
        self.assertTrue(keep_alive_enabled({CONF_AUTH_TYPE: AUTH_TYPE_COOKIE}, {}))

    def test_default_on_for_entry_without_username(self):
        # Entries from before auth_type was stored
        self.assertTrue(keep_alive_enabled({}, {}))

    def test_default_off_for_addon(self):
        for auth_type in (AUTH_TYPE_MYMETER, AUTH_TYPE_OKTA):
            data = {CONF_AUTH_TYPE: auth_type, CONF_USERNAME: "user@example.com"}
            self.assertFalse(keep_alive_enabled(data, {}))

    def test_option_overrides_default(self):
        addon = {CONF_AUTH_TYPE: AUTH_TYPE_MYMETER, CONF_USERNAME: "user@example.com"}
        self.assertTrue(keep_alive_enabled(addon, {CONF_KEEP_ALIVE: True}))
        self.assertFalse(keep_alive_enabled({CONF_AUTH_TYPE: AUTH_TYPE_COOKIE}, {CONF_KEEP_ALIVE: False}))


class TestKeepAliveDelay(unittest.TestCase):
    """Delays vary within the range instead of repeating one fixed interval."""

    def test_delay_within_range_and_varies(self):
        delays = {next_keep_alive_delay() for _ in range(50)}
        self.assertTrue(all(KEEPALIVE_MIN_SECONDS <= d <= KEEPALIVE_MAX_SECONDS for d in delays))
        self.assertGreater(len(delays), 1)

    def test_retry_comes_before_idle_timeout(self):
        # After the longest delay, a ping that fails is retried; the gap since the last
        # request that reached MyMeter must stay well under its ~10 minute idle timeout
        self.assertLess(KEEPALIVE_MAX_SECONDS + KEEPALIVE_RETRY_SECONDS, 8 * 60)


class TestIsLoginUrl(unittest.TestCase):
    """MyMeter sends an expired session to the portal root, where the sign-in form is."""

    def test_portal_root(self):
        self.assertTrue(is_login_url(f"{BASE_URL}/"))
        self.assertTrue(is_login_url(f"{BASE_URL}/?%3fServiceType%3d1"))
        self.assertTrue(is_login_url("/"))

    def test_login_paths(self):
        self.assertTrue(is_login_url(f"{BASE_URL}/Account/Login"))
        self.assertTrue(is_login_url("https://example.okta.com/signin"))
        self.assertTrue(is_login_url("https://login.example.com/authorize"))

    def test_dashboard(self):
        self.assertFalse(is_login_url(f"{BASE_URL}/Dashboard"))
        self.assertFalse(is_login_url(f"{BASE_URL}/Dashboard/ChartData?_=1"))


class TestClientKeepAlive(unittest.TestCase):
    """PsegClient.keep_alive makes one request and reports an expired session."""

    def setUp(self):
        self.client = PsegClient(cookie="MM_SID=abc; __RequestVerificationToken=tok")

    def test_valid_session(self):
        with patch.object(self.client.session, "get", return_value=response(200)) as get:
            self.client.keep_alive()
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], f"{BASE_URL}{KEEPALIVE_PATH}")
        # The sign-in page an expired session is redirected to is not downloaded
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_sent_as_an_xhr_with_an_empty_region(self):
        with patch.object(self.client.session, "get", return_value=response(200)) as get:
            self.client.keep_alive()
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Requested-With": "XMLHttpRequest"})
        # An empty value stays in the URL; a dropped one would be a different request
        url = requests.Request("GET", get.call_args.args[0], params=get.call_args.kwargs["params"]).prepare().url
        self.assertIn("Region=&", url)

    def test_signed_in_response(self):
        # Shape of a signed-in response: No widgets to insert, no redirect
        signed_in = {
            "AjaxResults": [{"Identifier": ".widget", "Action": "RemoveElement", "Value": None}],
            "Data": None,
        }
        with patch.object(self.client.session, "get", return_value=response(200, json_body=signed_in)):
            self.client.keep_alive()

    def test_json_redirect_is_expired(self):
        # What MyMeter answers an XHR of an expired session (checked after 16 idle minutes)
        redirect = {
            "AjaxResults": [{"Identifier": None, "Action": "Redirect", "Value": "/?%3fRegion%3d"}],
            "Data": None,
        }
        with patch.object(self.client.session, "get", return_value=response(200, json_body=redirect)):
            with self.assertRaises(PsegAuthError):
                self.client.keep_alive()

    def test_redirect_to_sign_in_is_expired(self):
        expired = response(302, location="/")
        with patch.object(self.client.session, "get", return_value=expired):
            with self.assertRaises(PsegAuthError):
                self.client.keep_alive()

    def test_unauthorized_is_expired(self):
        with patch.object(self.client.session, "get", return_value=response(401)):
            with self.assertRaises(PsegAuthError):
                self.client.keep_alive()

    def test_other_redirect_is_not_expired(self):
        moved = response(302, location=f"{BASE_URL}/Maintenance")
        with patch.object(self.client.session, "get", return_value=moved):
            with self.assertRaises(PsegConnectionError):
                self.client.keep_alive()

    def test_server_error_is_connection_error(self):
        with patch.object(self.client.session, "get", return_value=response(503)):
            with self.assertRaises(PsegConnectionError):
                self.client.keep_alive()

    def test_network_error_is_connection_error(self):
        with patch.object(self.client.session, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(PsegConnectionError):
                self.client.keep_alive()


class TestConnectionExpiry(unittest.TestCase):
    """test_connection follows redirects; landing on the portal root means expired."""

    def test_redirected_to_portal_root(self):
        client = PsegClient(cookie="MM_SID=abc")
        with patch.object(client.session, "get", return_value=response(200, url=f"{BASE_URL}/")):
            with self.assertRaises(PsegAuthError):
                client.test_connection()

    def test_dashboard_loaded(self):
        client = PsegClient(cookie="MM_SID=abc")
        with patch.object(client.session, "get", return_value=response(200)):
            self.assertTrue(client.test_connection())


class TestRenewedCookies(unittest.TestCase):
    """Cookies MyMeter renews via Set-Cookie replace the stored ones."""

    def test_host_only_renewal(self):
        client = PsegClient(cookie="MM_SID=old; __RequestVerificationToken=tok")
        client.session.cookies.set("MM_SID", "new", domain=HOST, path="/")
        with patch.object(client.session, "get", return_value=response(200)):
            client.keep_alive()
        self.assertEqual(client.cookie, "MM_SID=new; __RequestVerificationToken=tok")

    def test_domain_renewal_replaces_host_only_copy(self):
        client = PsegClient(cookie="MM_SID=old; __RequestVerificationToken=tok")
        # Set-Cookie with a Domain attribute is stored next to the host-only cookie
        client.session.cookies.set("MM_SID", "new", domain=f".{HOST}", path="/")
        with patch.object(client.session, "get", return_value=response(200)):
            client.keep_alive()
        self.assertEqual(
            sorted(client.cookie.split("; ")),
            ["MM_SID=new", "__RequestVerificationToken=tok"],
        )
        self.assertEqual([c.value for c in client.session.cookies if c.name == "MM_SID"], ["new"])

    def test_no_renewal_keeps_cookie(self):
        client = PsegClient(cookie="MM_SID=old; __RequestVerificationToken=tok")
        with patch.object(client.session, "get", return_value=response(200)):
            client.keep_alive()
        self.assertEqual(client.cookie, "MM_SID=old; __RequestVerificationToken=tok")

    def test_expired_session_keeps_cookie(self):
        client = PsegClient(cookie="MM_SID=old")
        client.session.cookies.set("MM_SID", "cleared", domain=f".{HOST}", path="/")
        with patch.object(client.session, "get", return_value=response(302, location="/")):
            with self.assertRaises(PsegAuthError):
                client.keep_alive()
        self.assertEqual(client.cookie, "MM_SID=old")


class TestCoordinatorKeepAlive(unittest.IsolatedAsyncioTestCase):
    """PsegDataUpdateCoordinator.async_keep_alive and what follows an expired session."""

    def coordinator(self, entry_data):
        hass = MagicMock()
        hass.is_stopping = False

        async def run_in_executor(func, *args):
            return func(*args)

        hass.async_add_executor_job = run_in_executor
        client = MagicMock()
        client.cookie = "MM_SID=abc"
        coordinator = PsegDataUpdateCoordinator(hass, client, entry_data)
        coordinator._raise_auth_issues = MagicMock()
        coordinator.async_request_refresh = AsyncMock()
        return coordinator

    def cookie_entry(self):
        return self.coordinator({CONF_AUTH_TYPE: AUTH_TYPE_COOKIE, CONF_COOKIE: "MM_SID=abc"})

    async def test_valid_session(self):
        coordinator = self.cookie_entry()
        self.assertTrue(await coordinator.async_keep_alive())
        coordinator.client.keep_alive.assert_called_once()
        self.assertFalse(coordinator.auth_required)
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_renewed_cookie_is_stored(self):
        coordinator = self.cookie_entry()
        coordinator.client.cookie = "MM_SID=new"
        await coordinator.async_keep_alive()
        self.assertEqual(coordinator.entry_data[CONF_COOKIE], "MM_SID=new")

    async def test_unreachable_portal_asks_for_a_retry(self):
        coordinator = self.cookie_entry()
        coordinator.client.keep_alive.side_effect = PsegConnectionError("timeout")
        self.assertFalse(await coordinator.async_keep_alive())
        self.assertFalse(coordinator.auth_required)
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_expired_session_starts_a_refresh(self):
        coordinator = self.cookie_entry()
        coordinator.client.keep_alive.side_effect = PsegAuthError("expired")
        self.assertTrue(await coordinator.async_keep_alive())
        self.assertTrue(coordinator.auth_required)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_expired_session_during_shutdown_is_left_alone(self):
        coordinator = self.cookie_entry()
        coordinator.hass.is_stopping = True
        coordinator.client.keep_alive.side_effect = PsegAuthError("expired")
        self.assertTrue(await coordinator.async_keep_alive())
        self.assertFalse(coordinator.auth_required)
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_no_ping_while_authentication_is_pending(self):
        coordinator = self.cookie_entry()
        coordinator.auth_required = True
        self.assertTrue(await coordinator.async_keep_alive())
        coordinator.client.keep_alive.assert_not_called()

    async def test_expired_cookie_asks_the_user_and_stops_pinging(self):
        coordinator = self.cookie_entry()
        coordinator.client.keep_alive.side_effect = PsegAuthError("expired")
        coordinator.client.fetch_energy_data.side_effect = PsegAuthError("expired")
        await coordinator.async_keep_alive()
        with self.assertRaises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()
        coordinator._raise_auth_issues.assert_called_once()
        await coordinator.async_keep_alive()
        coordinator.client.keep_alive.assert_called_once()

    async def test_expiry_during_the_reauth_cooldown_keeps_polling(self):
        coordinator = self.coordinator(
            {CONF_AUTH_TYPE: AUTH_TYPE_MYMETER, CONF_USERNAME: "user", CONF_PASSWORD: "secret"}
        )
        coordinator.client.keep_alive.side_effect = PsegAuthError("expired")
        coordinator.client.fetch_energy_data.side_effect = PsegAuthError("expired")
        coordinator._last_reauth_attempt = datetime.now(timezone.utc) - timedelta(seconds=15)
        await coordinator.async_keep_alive()
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        coordinator._raise_auth_issues.assert_not_called()
        await coordinator.async_keep_alive()
        coordinator.client.keep_alive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
