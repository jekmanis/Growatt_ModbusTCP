"""Test bootstrap for the integration modules.

``custom_components/growatt_modbus/*.py`` imports Home Assistant (and, for TCP,
pymodbus) at module scope, and neither package is installed in this repo's test
environment — ``tests/test_sensor_integrity.py`` works around that by parsing the
source instead of importing it.

Behavioural tests need the real objects, so this module installs minimal stand-ins
in ``sys.modules`` and loads the integration files under a synthetic package.
Loading them under a synthetic package (rather than importing
``custom_components.growatt_modbus``) keeps the integration's ``__init__.py`` — the
part that pulls in most of Home Assistant — out of the picture while still letting
``from .const import ...`` resolve.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "growatt_modbus"
PKG_NAME = "growatt_modbus_under_test"


# ---------------------------------------------------------------------------
# Stand-in third-party modules
# ---------------------------------------------------------------------------


class FakeModbusClient:
    """Just enough of a pymodbus client for GrowattModbus.__init__ to succeed.

    Tests replace the read/write methods they exercise; anything left untouched
    raises so an unnoticed real-device code path can't quietly pass.
    """

    def __init__(self, *args, **kwargs):
        self.socket = None
        self.timeout = kwargs.get("timeout", 10)
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        self.socket = object()
        return True

    def is_socket_open(self):
        return self.socket is not None

    def close(self):
        self.socket = None

    def read_holding_registers(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("read_holding_registers must be stubbed by the test")

    def read_input_registers(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("read_input_registers must be stubbed by the test")


def _new_module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_stub_packages() -> None:
    if "pymodbus" in sys.modules:
        return

    pymodbus = _new_module("pymodbus")
    pymodbus.__path__ = []  # mark as a package
    pymodbus_client = _new_module("pymodbus.client")
    pymodbus_client.ModbusTcpClient = FakeModbusClient
    pymodbus_client.ModbusSerialClient = FakeModbusClient
    pymodbus.client = pymodbus_client

    _new_module("serial")

    homeassistant = _new_module("homeassistant")
    homeassistant.__path__ = []

    config_entries = _new_module("homeassistant.config_entries")
    config_entries.ConfigEntry = type("ConfigEntry", (), {})
    homeassistant.config_entries = config_entries

    ha_const = _new_module("homeassistant.const")
    ha_const.CONF_HOST = "host"
    ha_const.CONF_PORT = "port"
    ha_const.CONF_NAME = "name"
    homeassistant.const = ha_const

    ha_core = _new_module("homeassistant.core")
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_core.callback = lambda func: func
    homeassistant.core = ha_core

    helpers = _new_module("homeassistant.helpers")
    helpers.__path__ = []
    homeassistant.helpers = helpers

    update_coordinator = _new_module("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        """Stand-in base class; only needs to be subscriptable and subclassable."""

        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    helpers.update_coordinator = update_coordinator

    ha_event = _new_module("homeassistant.helpers.event")
    ha_event.async_track_time_change = lambda *args, **kwargs: None
    helpers.event = ha_event

    ha_storage = _new_module("homeassistant.helpers.storage")
    ha_storage.Store = type("Store", (), {"__init__": lambda self, *a, **k: None})
    helpers.storage = ha_storage


def _load_component_module(name: str) -> types.ModuleType:
    """Import ``custom_components/growatt_modbus/<name>.py`` under a synthetic package."""
    _install_stub_packages()

    if PKG_NAME not in sys.modules:
        pkg = _new_module(PKG_NAME)
        pkg.__path__ = [str(COMPONENT_DIR)]

    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, COMPONENT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def growatt_modbus():
    """The ``growatt_modbus`` module (GrowattModbus, GrowattData, SharedModbusConnection)."""
    return _load_component_module("growatt_modbus")


@pytest.fixture(scope="session")
def component_const():
    """The integration's ``const`` module."""
    return _load_component_module("const")


@pytest.fixture(scope="session")
def coordinator_module():
    """The ``coordinator`` module (for GrowattModbusCoordinator._compute_wit_mode_status)."""
    return _load_component_module("coordinator")
