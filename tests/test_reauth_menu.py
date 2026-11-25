"""Tests for the re-authentication menu and Direct MyMeter prefill."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.pseg.const import CONF_USERNAME


class _Marker:
    def __init__(self, key, default=None):
        self.key = key
        self._default = default

    def default(self):
        return self._default


class _FakeVol:
    """Minimal voluptuous stand-in; the real package is not installed."""

    class Schema:
        def __init__(self, schema):
            self.schema = schema

    Required = _Marker
    Optional = _Marker

    @staticmethod
    def In(values):
        return values


class _FakeConfigFlow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


@pytest.fixture
def config_flow(monkeypatch):
    """Import config_flow with a real base class and a fake voluptuous."""
    config_entries = MagicMock()
    config_entries.ConfigFlow = _FakeConfigFlow
    monkeypatch.setitem(sys.modules, "voluptuous", _FakeVol)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setattr(
        sys.modules["homeassistant"], "config_entries", config_entries, raising=False
    )
    monkeypatch.setitem(sys.modules, "homeassistant.data_entry_flow", MagicMock())
    sys.modules.pop("custom_components.pseg.config_flow", None)
    module = importlib.import_module("custom_components.pseg.config_flow")
    yield module
    sys.modules.pop("custom_components.pseg.config_flow", None)


def _make_flow(config_flow, entry_data):
    flow = config_flow.PsegConfigFlow()
    entry = SimpleNamespace(data=entry_data, options={})
    flow.hass = MagicMock()
    flow.hass.config_entries.async_get_entry.return_value = entry
    flow.context = {"entry_id": "abc"}
    flow._reauth_entry = entry
    flow.async_show_menu = MagicMock(side_effect=lambda **kw: kw)
    flow.async_show_form = MagicMock(side_effect=lambda **kw: kw)
    return flow


@pytest.mark.parametrize("detected", ["http://addon:8000", None])
def test_reauth_menu_options(config_flow, detected):
    flow = _make_flow(config_flow, {CONF_USERNAME: "user"})
    flow._async_detect_auth_server = AsyncMock(return_value=detected)
    result = asyncio.run(flow.async_step_reauth({}))
    if detected:
        assert result["menu_options"] == [
            "automated_mymeter",
            "reauth_confirm",
            "cookie_mymeter",
            "cookie_okta",
        ]
    else:
        assert result["menu_options"] == ["cookie_mymeter", "cookie_okta"]


def test_automated_mymeter_prefills_reauth_username(config_flow):
    flow = _make_flow(config_flow, {CONF_USERNAME: "stored@example.com"})
    result = asyncio.run(flow.async_step_automated_mymeter())
    key = next(k for k in result["data_schema"].schema if k.key == CONF_USERNAME)
    assert key.default() == "stored@example.com"
