"""Tests for the options flow reload behavior."""

import importlib
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def config_flow(monkeypatch):
    """Import config_flow with a fake voluptuous."""
    monkeypatch.setitem(sys.modules, "voluptuous", MagicMock())
    monkeypatch.setitem(sys.modules, "homeassistant.data_entry_flow", MagicMock())
    sys.modules.pop("custom_components.pseg.config_flow", None)
    module = importlib.import_module("custom_components.pseg.config_flow")
    yield module
    sys.modules.pop("custom_components.pseg.config_flow", None)


def test_options_flow_inherits_reload_handler(config_flow):
    """The options flow uses Home Assistant's reload handler."""
    base = sys.modules["homeassistant.config_entries"].OptionsFlowWithReload
    assert issubclass(config_flow.PsegOptionsFlowHandler, base)


def test_no_update_listener_helper():
    """The module no longer exposes the old update listener."""
    pseg = importlib.import_module("custom_components.pseg")
    assert not hasattr(pseg, "async_reload_entry")
