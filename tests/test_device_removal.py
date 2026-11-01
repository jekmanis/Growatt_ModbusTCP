"""Stale devices can be deleted from the UI; live ones cannot.

Home Assistant shows "Delete" on a device only when the integration module defines
`async_remove_config_entry_device`, and calls it to ask. Without it, a leftover parent
registered as `(growatt_modbus, "<entry_id>")` - the format before device structure v2 -
stays in the registry for good, refused by both the UI and the API.

The cost of a wrong True is the user's live device and its entities, so these tests pin the
refusals as hard as the one case that is allowed.

The suffixes below are spelled out by hand on purpose. Deriving them from
const.ALL_DEVICE_TYPES would let a type that is dropped from that tuple vanish from the
tests at the same moment it became deletable.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"


def component(module: str):
    """conftest binds the component directory to the `growatt_under_test` package."""
    return importlib.import_module(f"growatt_under_test.{module}")


removal = component("device_removal")
const = component("const")

DOMAIN = "growatt_modbus"
ENTRY = "01JABCDEFGHJKMNPQRSTVWXYZ0"
OTHER_ENTRY = "01JZZZZZZZZZZZZZZZZZZZZZZZ"
DEVICE_ID = "dev-1"

# Every device get_device_info() builds today. Hand-maintained: see the module docstring.
CREATED_SUFFIXES = ["inverter", "solar", "grid", "load", "battery", "backup_box"]


class _State:
    def __init__(self, attributes=None):
        self.attributes = attributes or {}


class _Hass:
    def __init__(self, states=None):
        self._states = states or {}
        self.states = self

    def get(self, entity_id):
        return self._states.get(entity_id)


@pytest.fixture
def registry(monkeypatch):
    """Stand-in entity registry: a list of (device_id, entity_id)."""
    attached: list[tuple[str, str]] = []

    fake = types.ModuleType("homeassistant.helpers.entity_registry")
    fake.async_get = lambda hass: attached

    def async_entries_for_device(reg, device_id, include_disabled_entities=False):
        return [types.SimpleNamespace(entity_id=eid) for dev, eid in reg if dev == device_id]

    fake.async_entries_for_device = async_entries_for_device
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", fake)
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", fake,
                        raising=False)
    return attached


def _device(*identifiers, config_entries=(ENTRY,)):
    return types.SimpleNamespace(
        id=DEVICE_ID, name="Growatt", name_by_user=None,
        identifiers=set(identifiers), config_entries=set(config_entries),
    )


def _ask(device, hass=None, entry_id=ENTRY):
    entry = types.SimpleNamespace(entry_id=entry_id)
    return asyncio.run(
        removal.async_remove_config_entry_device(hass or _Hass(), entry, device)
    )


# --- the one case that is allowed -------------------------------------------------------

def test_legacy_bare_entry_id_parent_can_be_deleted(registry):
    assert _ask(_device((DOMAIN, ENTRY))) is True


def test_retired_suffix_of_this_entry_can_be_deleted(registry):
    assert _ask(_device((DOMAIN, f"{ENTRY}_retired_thing"))) is True


def test_restored_orphan_entity_does_not_block_deletion(registry):
    registry.append((DEVICE_ID, "sensor.growatt_old"))
    hass = _Hass({"sensor.growatt_old": _State({"restored": True})})
    assert _ask(_device((DOMAIN, ENTRY)), hass) is True


def test_disabled_entity_without_state_does_not_block_deletion(registry):
    registry.append((DEVICE_ID, "sensor.growatt_disabled"))
    assert _ask(_device((DOMAIN, ENTRY))) is True


# --- refusals ---------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", CREATED_SUFFIXES)
def test_every_device_the_integration_creates_is_protected(registry, suffix):
    assert _ask(_device((DOMAIN, f"{ENTRY}_{suffix}"))) is False


def test_current_identifier_protects_even_alongside_a_legacy_one(registry):
    assert _ask(_device((DOMAIN, ENTRY), (DOMAIN, f"{ENTRY}_inverter"))) is False


def test_live_entity_blocks_deletion(registry):
    registry.append((DEVICE_ID, "sensor.growatt_pv1_power"))
    hass = _Hass({"sensor.growatt_pv1_power": _State({"unit_of_measurement": "W"})})
    assert _ask(_device((DOMAIN, ENTRY)), hass) is False


def test_unavailable_but_not_restored_entity_counts_as_live(registry):
    registry.append((DEVICE_ID, "sensor.growatt_pv1_power"))
    hass = _Hass({"sensor.growatt_pv1_power": _State({})})
    assert _ask(_device((DOMAIN, ENTRY)), hass) is False


def test_device_of_another_entry_is_refused(registry):
    assert _ask(_device((DOMAIN, OTHER_ENTRY))) is False
    assert _ask(_device((DOMAIN, f"{OTHER_ENTRY}_inverter"))) is False


def test_device_of_another_domain_is_refused(registry):
    assert _ask(_device(("other_domain", ENTRY))) is False
    assert _ask(_device((DOMAIN, ENTRY), ("other_domain", "abc"))) is False


def test_device_not_attached_to_this_entry_is_refused(registry):
    assert _ask(_device((DOMAIN, ENTRY), config_entries=(OTHER_ENTRY,))) is False


def test_device_without_identifiers_is_refused(registry):
    assert _ask(_device()) is False


def test_error_while_deciding_refuses(registry, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(sys.modules["homeassistant.helpers.entity_registry"],
                        "async_entries_for_device", boom)
    assert _ask(_device((DOMAIN, ENTRY))) is False


# --- the live set cannot drift from the code that builds DeviceInfo ---------------------

def _coordinator_module():
    """coordinator.py imports homeassistant.util.dt, which conftest does not stub; in the
    full suite test_wit_entity_keys installs it at collection. Mirror that stub so this
    file also runs on its own - only over conftest's stand-in, never a real HA."""
    ha = sys.modules.get("homeassistant")
    if ha is not None and not getattr(ha, "__file__", None) and (
        "homeassistant.util" not in sys.modules
    ):
        util = types.ModuleType("homeassistant.util")
        util.__path__ = []
        dt_mod = types.ModuleType("homeassistant.util.dt")
        dt_mod.utcnow = lambda: None
        dt_mod.now = lambda: None
        dt_mod.as_local = lambda value: value
        util.dt = dt_mod
        ha.util = util
        sys.modules["homeassistant.util"] = util
        sys.modules["homeassistant.util.dt"] = dt_mod
    return component("coordinator")


def _fake_coordinator():
    maps = const.REGISTER_MAPS
    return types.SimpleNamespace(
        _register_map_key=next(iter(maps)),
        config={"name": "Growatt"},
        entry=types.SimpleNamespace(entry_id=ENTRY),
        _model_name="", _serial_number="", _firmware_version="", _firmware_build="",
        _protocol_version="",
        _via_parent_inverter=lambda entry_id: {},
    )


def test_hand_list_matches_all_device_types():
    assert sorted(const.ALL_DEVICE_TYPES) == sorted(CREATED_SUFFIXES)


@pytest.mark.parametrize("device_type", CREATED_SUFFIXES)
def test_get_device_info_builds_exactly_the_protected_identifier(device_type):
    coordinator = _coordinator_module()
    info =coordinator.GrowattModbusCoordinator.get_device_info(_fake_coordinator(), device_type)
    assert info["identifiers"] <= removal.current_device_identifiers(ENTRY)
    assert info["identifiers"] == {(DOMAIN, f"{ENTRY}_{device_type}")}


def test_every_device_type_get_device_info_branches_on_is_protected():
    """A seventh device added to get_device_info without ALL_DEVICE_TYPES would be
    deletable while in use. Read off the syntax tree: every DEVICE_TYPE_* the method
    compares against must be in the protected tuple."""
    source = (COMPONENT_DIR / "coordinator.py").read_text(encoding="utf-8")
    method = next(
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.FunctionDef) and n.name == "get_device_info"
    )
    compared = {
        getattr(const, side.id)
        for node in ast.walk(method) if isinstance(node, ast.Compare)
        for side in [node.left, *node.comparators]
        if isinstance(side, ast.Name) and side.id.startswith("DEVICE_TYPE_")
    }
    assert compared, "get_device_info no longer branches on DEVICE_TYPE_* - revisit this test"
    assert compared <= set(const.ALL_DEVICE_TYPES)


def test_no_identifier_is_built_outside_the_helper():
    """An identifier tuple written inline bypasses device_identifier(), and so bypasses
    the protection."""
    for name in ("coordinator.py", "select.py", "__init__.py", "entity.py"):
        source = (COMPONENT_DIR / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Tuple) and len(node.elts) == 2:
                first = node.elts[0]
                if isinstance(first, ast.Name) and first.id == "DOMAIN" and isinstance(
                    node.elts[1], ast.JoinedStr
                ):
                    raise AssertionError(f"{name}:{node.lineno} builds a device identifier inline")


def test_init_exposes_the_hook():
    """HA looks the hook up on the integration module; without it there is no Delete."""
    tree = ast.parse((COMPONENT_DIR / "__init__.py").read_text(encoding="utf-8"))
    exported = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "device_removal"
        for alias in node.names
    } | {
        node.name for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "async_remove_config_entry_device" in exported
