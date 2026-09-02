"""The entities an external consumer resolves by ID must survive the upstream merge.

An AppDaemon app (battery_optimizer) drives this integration and reads seven entities
by `entity_id`. It has no way to rediscover them: the IDs are configuration values in
its own config, so a rename is indistinguishable from the entity going away, and the app
either loses an input silently or stops scheduling.

    sensor.growatt_battery_battery_soc              <entry>_battery_soc
    sensor.growatt_solar_solar_total_power          <entry>_pv_total_power
    sensor.growatt_battery_battery_temperature      <entry>_battery_temp
    sensor.growatt_battery_battery_charge_today     <entry>_battery_charge_today
    sensor.growatt_battery_battery_discharge_today  <entry>_battery_discharge_today
    sensor.growatt_load_house_consumption           <entry>_house_consumption
    sensor.growatt_inverter_mode                    <entry>_wit_mode_status

Four independent things have to hold for those IDs to stay put, and each has its own
test below:

1. The key is in `SENSOR_DEFINITIONS` *and* in the WIT profile's sensor set. `sensor.py`
   creates exactly that intersection, and `__init__.async_setup_entry`'s blanket
   stale-entity rule *removes from the registry* anything in SENSOR_DEFINITIONS that the
   profile does not list. So dropping a key from the profile does not leave an
   unavailable entity to notice - it deletes it.

2. `SENSOR_DEFINITIONS[key]["name"]` and `get_device_type_for_sensor(key)` are unchanged.
   These are not cosmetic. `__init__._migrate_entity_ids` runs on *every* setup and
   renames each registry row to `{domain}.{slug(device name)}_{slug(short name)}` derived
   from exactly those two values. Editing a `name` string renames a live entity.

3. `wit_mode_status` is built by `GrowattWitModeStatusSensor` and *only* by it. The key
   is in `device_profiles.STATUS_SENSORS`, so it passes the profile check in the generic
   loop as well; without the `custom_class_sensors` exclusion the same unique ID would be
   produced twice - once at setup and once by the deferred-sensor listener.

4. The unique-ID suffix itself. Upstream v1.8.14 moved every entity onto
   `entity.GrowattEntity`, which composes `f"{entry_id}_{unique_key}"`. The re-ported
   fork class has to pass the same suffix it used when it hand-rolled `_attr_unique_id`,
   and must NOT keep its old entry-name prefix in `_attr_name` - `GrowattEntity` sets
   `has_entity_name = True`, so Home Assistant adds the device name itself.

The sensor platform is imported for real rather than source-scanned: the definitions are
assembled from template helpers (`_pv_string_sensors` and friends), so a regex over the
source cannot see every key, and the values these tests pin are exactly the ones such a
regex would have to re-derive. That costs a few more Home Assistant stubs than
`tests/conftest.py` installs; they are added below, and only when the conftest's stub is
the one in play.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"

PROFILE = "wit_4000_15000tl3"

# Facts from the reference installation (one config entry, device name "Growatt").
# key -> (live entity_id, short name, device type). The unique_id suffix is the key.
OPTIMIZER_SENSORS = {
    "battery_soc": (
        "sensor.growatt_battery_battery_soc", "Battery SOC", "battery",
    ),
    "pv_total_power": (
        "sensor.growatt_solar_solar_total_power", "Solar Total Power", "solar",
    ),
    "battery_temp": (
        "sensor.growatt_battery_battery_temperature", "Battery Temperature", "battery",
    ),
    "battery_charge_today": (
        "sensor.growatt_battery_battery_charge_today", "Battery Charge Today", "battery",
    ),
    "battery_discharge_today": (
        "sensor.growatt_battery_battery_discharge_today", "Battery Discharge Today",
        "battery",
    ),
    "house_consumption": (
        "sensor.growatt_load_house_consumption", "House Consumption", "load",
    ),
    "wit_mode_status": (
        "sensor.growatt_inverter_mode", "Inverter Mode", "inverter",
    ),
}

DEVICE_NAME = "Growatt"


# ---------------------------------------------------------------------------
# Home Assistant stubs the sensor platform needs on top of conftest's set.
#
# conftest stubs what growatt_modbus.py and coordinator.py import. sensor.py
# additionally imports the sensor component, the entity helpers and the unit constants.
# Everything added here is attribute-passthrough: the values only end up in dict literals
# built at import time, and no test compares them.
# ---------------------------------------------------------------------------


class _NameSpaceMeta(type):
    """Class whose every attribute access yields a stable placeholder string."""

    def __getattr__(cls, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        value = f"{cls.__name__}.{name}"
        setattr(cls, name, value)
        return value


def _namespace(name: str):
    return _NameSpaceMeta(name, (), {})


def _install_sensor_platform_stubs() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is None:  # pragma: no cover - conftest always imports or stubs it
        if importlib.util.find_spec("homeassistant") is not None:
            return
        pytest.skip("conftest did not install the homeassistant stub")
    # A real Home Assistant is installed (the tests_ha/ suite): nothing to do, and
    # shadowing it here would be actively harmful. conftest's stand-in is a bare
    # ModuleType, which has no __file__. find_spec() cannot be used for this - it raises
    # on a sys.modules entry whose __spec__ is None, which is exactly the stub's shape.
    if getattr(ha, "__file__", None):  # pragma: no cover - no HA in the fast suite
        return

    components = sys.modules.get("homeassistant.components")
    if components is None:
        components = types.ModuleType("homeassistant.components")
        components.__path__ = []
        ha.components = components
        sys.modules["homeassistant.components"] = components

    if "homeassistant.components.sensor" not in sys.modules:
        sensor_mod = types.ModuleType("homeassistant.components.sensor")
        sensor_mod.SensorDeviceClass = _namespace("SensorDeviceClass")
        sensor_mod.SensorStateClass = _namespace("SensorStateClass")

        class SensorEntity:  # noqa: D401 - stand-in base
            """Placeholder base; the platform only sets _attr_* on it."""

        sensor_mod.SensorEntity = SensorEntity
        components.sensor = sensor_mod
        sys.modules["homeassistant.components.sensor"] = sensor_mod

    helpers = sys.modules["homeassistant.helpers"]

    if "homeassistant.helpers.entity" not in sys.modules:
        entity_mod = types.ModuleType("homeassistant.helpers.entity")
        entity_mod.EntityCategory = _namespace("EntityCategory")
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
            """Placeholder base for entity.GrowattEntity.

            Subscriptable because entity.py declares
            ``class GrowattEntity(CoordinatorEntity[GrowattModbusCoordinator])``.
            """

            def __init__(self, coordinator, context=None):
                self.coordinator = coordinator

            def __class_getitem__(cls, item):
                return cls

        update_coordinator.CoordinatorEntity = CoordinatorEntity

    if "homeassistant.util" not in sys.modules:
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

    ha_const = sys.modules["homeassistant.const"]
    ha_const.PERCENTAGE = "%"
    for unit in (
        "UnitOfApparentPower", "UnitOfElectricCurrent", "UnitOfElectricPotential",
        "UnitOfEnergy", "UnitOfFrequency", "UnitOfPower", "UnitOfTemperature",
        "UnitOfTime",
    ):
        if not hasattr(ha_const, unit):
            setattr(ha_const, unit, _namespace(unit))


_install_sensor_platform_stubs()

_sensor = importlib.import_module("growatt_under_test.sensor")
_const = importlib.import_module("growatt_under_test.const")
_device_profiles = importlib.import_module("growatt_under_test.device_profiles")

SENSOR_DEFINITIONS = _sensor.SENSOR_DEFINITIONS
WIT_SENSORS = set(_device_profiles.get_sensors_for_profile(PROFILE))


def _slugify(text: str) -> str:
    """The subset of homeassistant.util.slugify these names exercise."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _migrated_entity_id(key: str) -> str:
    """Reproduce __init__._migrate_entity_ids for one sensor key."""
    suffix = {
        "solar": "Solar", "grid": "Grid", "load": "Load", "battery": "Battery",
    }.get(_const.get_device_type_for_sensor(key))
    device_name = f"{DEVICE_NAME} {suffix}" if suffix else DEVICE_NAME
    short_name = SENSOR_DEFINITIONS[key]["name"]
    return f"sensor.{_slugify(device_name)}_{_slugify(short_name)}"


def _setup_entry_source() -> str:
    source = (COMPONENT_DIR / "sensor.py").read_text(encoding="utf-8")
    match = re.search(
        r"^async def async_setup_entry\(.*?(?=^async def |^class |\Z)",
        source, re.MULTILINE | re.DOTALL,
    )
    assert match, "async_setup_entry not found in sensor.py"
    return match.group(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(OPTIMIZER_SENSORS))
def test_key_is_defined_and_in_the_wit_profile(key: str) -> None:
    """sensor.py builds SENSOR_DEFINITIONS n profile, and deletes the rest.

    Missing from either side is not a dormant entity: the blanket cleanup in
    __init__.async_setup_entry removes the registry row outright.
    """
    assert key in SENSOR_DEFINITIONS, (
        f"{key} has no SENSOR_DEFINITIONS entry, so no sensor entity can be created and "
        f"the existing registry row is removed as stale"
    )
    assert key in WIT_SENSORS, (
        f"{key} is not in get_sensors_for_profile({PROFILE!r}); the stale-sensor rule in "
        f"__init__.async_setup_entry removes {OPTIMIZER_SENSORS[key][0]} from the entity "
        f"registry on the next setup"
    )


@pytest.mark.parametrize("key", sorted(OPTIMIZER_SENSORS))
def test_short_name_and_device_type_are_unchanged(key: str) -> None:
    """Both feed _migrate_entity_ids, which renames live entities on every setup."""
    _eid, expected_name, expected_device = OPTIMIZER_SENSORS[key]
    assert SENSOR_DEFINITIONS[key]["name"] == expected_name
    assert _const.get_device_type_for_sensor(key) == expected_device


@pytest.mark.parametrize("key", sorted(OPTIMIZER_SENSORS))
def test_migration_reproduces_the_live_entity_id(key: str) -> None:
    """_migrate_entity_ids must compute the ID the installation already has."""
    assert _migrated_entity_id(key) == OPTIMIZER_SENSORS[key][0]


def test_wit_mode_status_is_built_once() -> None:
    """Its key passes the generic loop's profile check, so it must be excluded there.

    wit_mode_status is in device_profiles.STATUS_SENSORS (nearly every profile includes
    that group), so `sensor_key not in available_sensors` does not skip it. Without the
    exclusion the generic loop and GrowattWitModeStatusSensor both produce
    `{entry_id}_wit_mode_status`, and the deferred listener adds a third once real data
    arrives.
    """
    body = _setup_entry_source()
    match = re.search(r"custom_class_sensors\s*=\s*\{([^}]*)\}", body)
    assert match, "custom_class_sensors set not found in sensor.async_setup_entry"
    excluded = set(re.findall(r'"([a-z0-9_]+)"', match.group(1)))
    assert "wit_mode_status" in excluded

    assert re.search(r"if sensor_key in custom_class_sensors:\s*\n\s*continue", body), (
        "the generic creation loop no longer skips custom_class_sensors"
    )
    assert "not in custom_class_sensors" in body, (
        "the deferred-sensor list no longer excludes custom_class_sensors, so the "
        "listener will add a duplicate unique_id once real data arrives"
    )
    assert re.search(
        r"if is_wit:\s*\n\s*entities\.append\(GrowattWitModeStatusSensor", body
    ), (
        "GrowattWitModeStatusSensor is no longer instantiated for WIT profiles, so "
        "sensor.growatt_inverter_mode is never created"
    )


def test_bespoke_sensor_classes_are_excluded_from_created_keys() -> None:
    """created_keys must filter by type, not by hasattr.

    GrowattWitModeStatusSensor and GrowattInverterClockSensor carry no `_sensor_key`;
    iterating every entity raised AttributeError and aborted setup in v1.8.6-v1.8.8.
    """
    body = _setup_entry_source()
    assert re.search(
        r"created_keys\s*=\s*\{\s*e\._sensor_key for e in entities\s+"
        r"if isinstance\(e, GrowattModbusSensor\)", body
    ), "created_keys no longer restricts itself to GrowattModbusSensor instances"


def test_wit_mode_status_sensor_unique_id_and_name() -> None:
    """The unique ID suffix is the anchor for sensor.growatt_inverter_mode.

    Also guards the has_entity_name migration: GrowattEntity sets
    `_attr_has_entity_name = True`, so a name that still carried the entry prefix would
    render as "Growatt Growatt Inverter Mode".
    """
    entry_id = "01KBB0DFK8WSEB83341HYNM1MX"

    class _Entry:
        def __init__(self) -> None:
            self.entry_id = entry_id
            self.title = DEVICE_NAME
            self.data = {"name": DEVICE_NAME}

    class _Coordinator:
        data = None

        def get_device_info(self, device_type):
            return {"identifiers": {("growatt_modbus", f"{entry_id}_{device_type}")}}

    entity = _sensor.GrowattWitModeStatusSensor(_Coordinator(), _Entry())

    assert entity._attr_unique_id == f"{entry_id}_wit_mode_status"
    assert entity._device_type == "inverter"
    assert entity._attr_name == "Inverter Mode"
    assert entity._attr_has_entity_name is True
    assert not hasattr(entity, "_sensor_key")


def test_no_two_sensor_keys_share_a_unique_id() -> None:
    """A bespoke class hard-coding a suffix already used by a definition would collide.

    Dict keys cannot collide with each other, so the only risk is a bespoke sensor class.
    There are two: wit_mode_status (excluded from the generic loop, checked above) and
    inverter_clock, which must not be a SENSOR_DEFINITIONS key at all.
    """
    assert "inverter_clock" not in SENSOR_DEFINITIONS, (
        "inverter_clock is built by GrowattInverterClockSensor; a SENSOR_DEFINITIONS "
        "entry would create a second entity with the same unique_id"
    )
