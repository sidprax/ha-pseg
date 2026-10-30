"""Pytest fixtures and environment configuration."""

from datetime import datetime, timezone
import sys
from unittest.mock import MagicMock

class FakeDataUpdateCoordinator:
    """Mock base class so coordinator subclasses behave as real instances."""
    def __init__(self, hass, logger, *, name=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None

    def __class_getitem__(cls, item):
        return cls

# Stub Home Assistant packages if not installed in current environment
for mod in [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.frontend",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.sensor",
    "homeassistant.components.button",
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

class FakeClientError(Exception):
    """Mock aiohttp.ClientError base class."""

class FakeClientConnectorError(FakeClientError):
    """Mock aiohttp.ClientConnectorError so exceptions derive from BaseException."""
    def __init__(self, connection_key=None, os_error=None, *args, **kwargs):
        self.os_error = os_error
        super().__init__(os_error, *args)

if "aiohttp" not in sys.modules:
    aio = MagicMock()
    aio.ClientError = FakeClientError
    aio.ClientConnectorError = FakeClientConnectorError
    sys.modules["aiohttp"] = aio
else:
    aio = sys.modules["aiohttp"]
    aio.ClientError = FakeClientError
    aio.ClientConnectorError = FakeClientConnectorError

# Wire package hierarchy so submodule imports resolve to their configured stubs
sys.modules["homeassistant"].util = sys.modules["homeassistant.util"]
sys.modules["homeassistant.util"].dt = sys.modules["homeassistant.util.dt"]
sys.modules["homeassistant"].helpers = sys.modules["homeassistant.helpers"]
sys.modules["homeassistant.helpers"].aiohttp_client = sys.modules["homeassistant.helpers.aiohttp_client"]
sys.modules["homeassistant.helpers"].update_coordinator = sys.modules["homeassistant.helpers.update_coordinator"]
sys.modules["homeassistant"].components = sys.modules["homeassistant.components"]
sys.modules["homeassistant.components"].persistent_notification = sys.modules["homeassistant.components.persistent_notification"]

# Configure specific stubs
coord_mod = sys.modules["homeassistant.helpers.update_coordinator"]
coord_mod.DataUpdateCoordinator = FakeDataUpdateCoordinator
coord_mod.UpdateFailed = Exception

dt_mod = sys.modules["homeassistant.util.dt"]
dt_mod.utcnow.side_effect = lambda: datetime.now(timezone.utc)
dt_mod.now.side_effect = lambda: datetime.now(timezone.utc)
dt_mod.as_utc.side_effect = lambda d: d
dt_mod.UTC = timezone.utc
