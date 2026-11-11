"""Tests for selecting the electric service before fetching usage."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401  (stubs Home Assistant for direct unittest invocation)

from custom_components.pseg.const import PORTAL_URL_LI, PORTAL_URL_NJ
from custom_components.pseg.pseg_client import PsegClient, PsegConnectionError

# Literals on purpose: the tests must fail on behavior, not on a missing constant.
SERVICE_TYPE_ELECTRIC = 1
SERVICE_TYPE_URL = "/Dashboard/SetServiceType"


class StopFetch(Exception):
    """Ends a fetch right after the dashboard token request."""


def redirect_to_dashboard(portal_url: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 302
    resp.is_redirect = True
    resp.headers = {"Location": f"{portal_url}/Dashboard"}
    return resp


def run_fetch(client: PsegClient, fetch: str) -> list:
    """Run a fetch until the token request; return the GET and token calls in order."""
    calls = []

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return redirect_to_dashboard(client.base_url)

    def token():
        calls.append(("token",))
        raise StopFetch

    with patch.object(client.session, "get", side_effect=get), patch.object(
        client, "_get_verification_token", side_effect=token
    ):
        try:
            if fetch == "fetch_history_range":
                client.fetch_history_range("2026-10-01", "2026-10-31")
            else:
                client.fetch_energy_data()
        except StopFetch:
            return calls
    raise AssertionError(f"{fetch} did not request the dashboard token")


class TestServiceTypeSelection(unittest.TestCase):
    """The NJ portal is switched to the electric service before each fetch."""

    def test_nj_selects_electric(self):
        self.assertEqual(getattr(PsegClient(cookie="MM_SID=abc"), "service_type", None), SERVICE_TYPE_ELECTRIC)

    def test_nj_with_trailing_slash_selects_electric(self):
        client = PsegClient(cookie="MM_SID=abc", portal_url=f"{PORTAL_URL_NJ}/")
        self.assertEqual(getattr(client, "service_type", None), SERVICE_TYPE_ELECTRIC)

    def test_both_fetches_select_the_service_before_the_dashboard(self):
        for fetch in ("fetch_energy_data", "fetch_history_range"):
            with self.subTest(fetch=fetch):
                calls = run_fetch(PsegClient(cookie="MM_SID=abc"), fetch)
                self.assertEqual(len(calls), 2)
                kind, url, kwargs = calls[0]
                self.assertEqual(kind, "get")
                self.assertEqual(url, f"{PORTAL_URL_NJ}{SERVICE_TYPE_URL}")
                self.assertEqual(kwargs["params"], {"ServiceType": SERVICE_TYPE_ELECTRIC})
                self.assertFalse(kwargs["allow_redirects"])
                self.assertEqual(calls[1], ("token",))

    def test_long_island_is_left_alone(self):
        client = PsegClient(cookie="MM_SID=abc", portal_url=PORTAL_URL_LI)
        self.assertIsNone(getattr(client, "service_type", None))
        for fetch in ("fetch_energy_data", "fetch_history_range"):
            with self.subTest(fetch=fetch):
                self.assertEqual(run_fetch(client, fetch), [("token",)])

    def test_rejected_selection_logs_a_warning_and_fetch_goes_on(self):
        client = PsegClient(cookie="MM_SID=abc")
        resp = MagicMock()
        resp.status_code = 404
        with patch.object(client.session, "get", return_value=resp), patch.object(
            client, "_get_verification_token", side_effect=StopFetch
        ) as token, self.assertLogs(
            "custom_components.pseg.pseg_client", level="WARNING"
        ) as logs, self.assertRaises(StopFetch):
            client.fetch_energy_data()
        token.assert_called_once()
        self.assertIn("HTTP 404", logs.output[0])

    def test_network_error_is_a_connection_error(self):
        client = PsegClient(cookie="MM_SID=abc")
        with patch.object(
            client.session, "get", side_effect=requests.ConnectionError("down")
        ), patch.object(client, "_get_verification_token") as token, self.assertRaises(
            PsegConnectionError
        ):
            client.fetch_energy_data()
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
