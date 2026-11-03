"""Tests for MyMeter portal selection."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tests.conftest  # noqa: F401  (stubs Home Assistant for direct unittest invocation)

from custom_components.pseg.const import (
    CONF_PORTAL_URL,
    DEFAULT_PORTAL_URL,
    PORTAL_URL_LI,
    PORTAL_URL_NJ,
)
from custom_components.pseg.pseg_client import KEEPALIVE_PATH, PsegClient
from custom_components.pseg.util import get_portal_url


def response(url: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.url = url
    resp.is_redirect = False
    resp.json.side_effect = ValueError("not JSON")
    return resp


class TestGetPortalUrl(unittest.TestCase):
    """Entries keep their portal; older entries without one are NJ."""

    def test_entry_without_portal_is_nj(self):
        self.assertEqual(get_portal_url({}), PORTAL_URL_NJ)
        self.assertEqual(get_portal_url(None), PORTAL_URL_NJ)
        self.assertEqual(get_portal_url({CONF_PORTAL_URL: ""}), PORTAL_URL_NJ)

    def test_stored_portal(self):
        self.assertEqual(get_portal_url({CONF_PORTAL_URL: PORTAL_URL_LI}), PORTAL_URL_LI)


class TestClientPortal(unittest.TestCase):
    """PsegClient sends every request to its portal."""

    def test_default_is_nj(self):
        client = PsegClient(cookie="MM_SID=abc")
        self.assertEqual(client.base_url, DEFAULT_PORTAL_URL)

    def test_trailing_slash_is_dropped(self):
        client = PsegClient(cookie="MM_SID=abc", portal_url=f"{PORTAL_URL_LI}/")
        self.assertEqual(client.base_url, PORTAL_URL_LI)

    def test_cookies_and_headers_belong_to_the_portal(self):
        client = PsegClient(cookie="MM_SID=abc", portal_url=PORTAL_URL_LI)
        self.assertEqual(
            {c.domain for c in client.session.cookies}, {"mysmartenergy.psegliny.com"}
        )
        self.assertEqual(client.session.headers["Referer"], f"{PORTAL_URL_LI}/Dashboard")

    def test_requests_go_to_the_portal(self):
        client = PsegClient(cookie="MM_SID=abc", portal_url=PORTAL_URL_LI)
        with patch.object(
            client.session, "get", return_value=response(f"{PORTAL_URL_LI}/Dashboard")
        ) as get:
            client.test_connection()
            client.keep_alive()
        self.assertEqual(
            [c.args[0] for c in get.call_args_list],
            [f"{PORTAL_URL_LI}/Dashboard", f"{PORTAL_URL_LI}{KEEPALIVE_PATH}"],
        )


if __name__ == "__main__":
    unittest.main()
