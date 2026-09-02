"""The WIT switch platform: which profiles get it, what it is called, what it publishes.

switch.py is fork-only, so upstream's platform tests do not cover it and it was re-ported
onto `GrowattEntity` by hand. Three things went wrong in that port, each invisible at
import time:

1. It gated on `== "WIT_4000_15000TL3"` while select.py, number.py and time.py gate on
   both WIT maps. On WIT_29900_50000TL3_XHU - which carries 30201 and 30411 exactly like
   the other map - the mode sensor, the Mode Preset select and the VPP numbers were
   created and these two switches silently were not.
2. `_attr_name = "Grid Export"` on the Grid sub-device. `GrowattEntity` sets
   `has_entity_name = True`, so Home Assistant composes device + entity: "Growatt Grid"
   + "Grid Export" = "Growatt Grid Grid Export", where the entity previously read
   "Growatt Grid Export".
3. `is_on` read `vpp_export_limit_power_rate > 0` with no availability check. The VPP
   holding blocks are best-effort reads and GrowattData is rebuilt per poll, so a missed
   30200-30201 read leaves the dataclass default 0 and the switch flips to `off` - which
   states that export is blocked, about a limiter the inverter has not touched. That is
   the fabricated-reading defect that froze the mode sensor at "Passthrough", one entity
   over, and the gating the other control platforms already have.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
ENTRY_ID = "01KBB0DFK8WSEB83341HYNM1MX"
DEVICE_NAME = "Growatt"


def _install_switch_platform_stubs() -> None:
    """Home Assistant pieces switch.py imports, on top of conftest's set.

    Idempotent and additive: every name is a placeholder that only has to exist. Skipped
    entirely when a real Home Assistant is installed (the tests_ha/ suite), where
    shadowing it would be actively harmful.
    """
    ha = sys.modules.get("homeassistant")
    if ha is None:  # pragma: no cover - conftest always imports or stubs it
        if importlib.util.find_spec("homeassistant") is not None:
            return
        pytest.skip("conftest did not install the homeassistant stub")
    if getattr(ha, "__file__", None):  # pragma: no cover - no HA in the fast suite
        return

    components = sys.modules.get("homeassistant.components")
    if components is None:
        components = types.ModuleType("homeassistant.components")
        components.__path__ = []
        ha.components = components
        sys.modules["homeassistant.components"] = components

    if "homeassistant.components.switch" not in sys.modules:
        switch_mod = types.ModuleType("homeassistant.components.switch")

        class SwitchEntity:  # noqa: D401 - stand-in base
            """Placeholder base; the platform only sets _attr_* on it."""

        switch_mod.SwitchEntity = SwitchEntity
        components.switch = switch_mod
        sys.modules["homeassistant.components.switch"] = switch_mod

    helpers = sys.modules["homeassistant.helpers"]

    if "homeassistant.helpers.entity" not in sys.modules:
        entity_mod = types.ModuleType("homeassistant.helpers.entity")

        class EntityCategory:  # noqa: D401 - stand-in enum
            """Placeholder; only the attribute name is referenced."""

            CONFIG = "config"
            DIAGNOSTIC = "diagnostic"

        entity_mod.EntityCategory = EntityCategory
        helpers.entity = entity_mod
        sys.modules["homeassistant.helpers.entity"] = entity_mod

    if "homeassistant.helpers.entity_platform" not in sys.modules:
        platform_mod = types.ModuleType("homeassistant.helpers.entity_platform")
        platform_mod.AddEntitiesCallback = object
        helpers.entity_platform = platform_mod
        sys.modules["homeassistant.helpers.entity_platform"] = platform_mod

    update_coordinator = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(update_coordinator, "CoordinatorEntity"):
        class CoordinatorEntity:  # noqa: D401 - stand-in base
            """Placeholder base for entity.GrowattEntity."""

            def __init__(self, coordinator, context=None):
                self.coordinator = coordinator

            def __class_getitem__(cls, item):
                return cls

        update_coordinator.CoordinatorEntity = CoordinatorEntity

    # `available` specifically, whether the stub came from here or from another test
    # module that got there first. `GrowattWitExportSwitch.available` chains to it, which
    # is the whole point of the override - a control must not be published as available on
    # a failed poll - and the real CoordinatorEntity always has it.
    base = update_coordinator.CoordinatorEntity
    if not hasattr(base, "available"):
        base.available = property(
            lambda self: getattr(self.coordinator, "last_update_success", True)
        )


_install_switch_platform_stubs()

_switch = importlib.import_module("growatt_under_test.switch")
_const = importlib.import_module("growatt_under_test.const")
_gm = importlib.import_module("growatt_under_test.growatt_modbus")


class _Entry:
    def __init__(self) -> None:
        self.entry_id = ENTRY_ID
        self.title = DEVICE_NAME
        self.data = {"name": DEVICE_NAME}


class _Coordinator:
    def __init__(self, data=None) -> None:
        self.data = data
        self.last_update_success = True

    def get_device_info(self, device_type):
        suffix = {"grid": " Grid", "battery": " Battery"}.get(device_type, "")
        return {
            "identifiers": {("growatt_modbus", f"{ENTRY_ID}_{device_type}")},
            "name": f"{DEVICE_NAME}{suffix}",
        }


def _export_switch(data=None):
    return _switch.GrowattWitExportSwitch(_Coordinator(data), _Entry())


def _data(**kwargs):
    return _gm.GrowattData(**kwargs)


# ---------------------------------------------------------------------------
# 1. Profile gating
# ---------------------------------------------------------------------------


def test_the_switch_platform_gates_on_both_wit_profiles() -> None:
    source = (COMPONENT_DIR / "switch.py").read_text(encoding="utf-8")
    assert "in WIT_REGISTER_MAPS" in source, (
        "switch.py compares against one map name again; on the other WIT profile its two "
        "entities silently disappear while every other WIT entity is created"
    )
    assert '== "WIT_4000_15000TL3"' not in source


@pytest.mark.parametrize("platform", ["switch.py", "select.py", "number.py", "time.py"])
def test_every_wit_platform_uses_the_same_gate(platform: str) -> None:
    """One tuple, in const.py, so the four cannot drift apart again."""
    source = (COMPONENT_DIR / platform).read_text(encoding="utf-8")
    assert "WIT_REGISTER_MAPS" in source, f"{platform} has its own WIT profile list"


def test_the_gate_covers_the_profiles_that_carry_the_registers() -> None:
    """The tuple is not arbitrary: both maps carry the registers the switches write."""
    for name in _const.WIT_REGISTER_MAPS:
        holding = _const.REGISTER_MAPS[name].get("holding_registers", {})
        assert _switch.VPP_EXPORT_LIMIT_POWER_RATE in holding, name
        assert _switch.VPP_TOU_NUM_PERIODS in holding, name


# ---------------------------------------------------------------------------
# 2. Naming
# ---------------------------------------------------------------------------


def test_the_export_switch_keeps_its_unique_id() -> None:
    """switch.growatt_grid_export is anchored on this suffix, and no migration renames
    switch entities - `_migrate_entity_ids` covers sensor, binary_sensor, number, select
    and time only."""
    assert _export_switch()._attr_unique_id == f"{ENTRY_ID}_grid_export_switch"


def test_the_export_switch_does_not_stutter_the_device_name() -> None:
    """has_entity_name composes device + entity. On the Grid sub-device that made
    "Grid Export" render as "Growatt Grid Grid Export"."""
    entity = _export_switch()
    device_name = entity.device_info["name"]
    assert device_name == "Growatt Grid"
    assert f"{device_name} {entity._attr_name}" == "Growatt Grid Export"


def test_the_optimizer_switch_name_is_unchanged() -> None:
    """It sits on the inverter device, which has no suffix, so it was never affected."""
    entity = _switch.GrowattWitOptimizerSwitch(_Coordinator(), _Entry())
    assert entity._attr_unique_id == f"{ENTRY_ID}_battery_optimizer_switch"
    assert f"{entity.device_info['name']} {entity._attr_name}" == "Growatt Battery Optimizer"


# ---------------------------------------------------------------------------
# 3. Never publish a state for a block that was not read
# ---------------------------------------------------------------------------


def test_export_state_is_published_when_the_block_answered() -> None:
    on = _export_switch(_data(vpp_export_limit_available=True,
                              vpp_export_limit_power_rate=100))
    assert on.available is True
    assert on.is_on is True

    off = _export_switch(_data(vpp_export_limit_available=True,
                               vpp_export_limit_power_rate=0))
    assert off.available is True
    assert off.is_on is False


def test_a_missed_block_is_unavailable_not_off() -> None:
    """The failing case: the inverter is exporting at 100 %, one 30200-30201 read is
    dropped, and the switch would report `off` - "export is blocked" - about a limiter
    nothing has touched."""
    entity = _export_switch(_data(vpp_export_limit_available=False,
                                  vpp_export_limit_power_rate=0))
    assert entity.available is False
    assert entity.is_on is None


def test_the_default_dataclass_is_not_a_reading() -> None:
    """A fresh GrowattData is what a poll that skipped the block leaves behind."""
    entity = _export_switch(_data())
    assert entity.available is False
    assert entity.is_on is None


def test_no_data_at_all_is_unavailable() -> None:
    entity = _export_switch(None)
    assert entity.available is False
    assert entity.is_on is None


def test_the_optimizer_switch_stays_reachable_while_the_link_is_down() -> None:
    """Deliberately NOT gated on the coordinator: it is the manual kill switch for the
    AppDaemon optimizer and is most needed exactly when polling is failing."""
    coordinator = _Coordinator(_data())
    coordinator.last_update_success = False
    entity = _switch.GrowattWitOptimizerSwitch(coordinator, _Entry())

    class _States:
        @staticmethod
        def get(entity_id):
            return types.SimpleNamespace(state="on")

    entity.hass = types.SimpleNamespace(states=_States())
    assert entity.available is True
    assert entity.is_on is True
