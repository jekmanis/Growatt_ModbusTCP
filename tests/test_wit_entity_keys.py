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


# ---------------------------------------------------------------------------
# Part 2: the whole registry, not just the seven keys.
#
# The tests above pin the seven IDs the AppDaemon app resolves. They cannot see the
# other 127 rows, and three of the mechanisms involved act on every row at once:
#
#   * `__init__.async_setup_entry` DELETES any sensor whose key the profile no longer
#     lists, and any number/select/time control whose register the profile no longer
#     maps (or now marks read-only).
#   * `__init__._migrate_entity_ids` RENAMES every row it can find, on every setup,
#     from `SENSOR_DEFINITIONS[key]["name"]` or a `.title()` of the control name.
#   * a platform can emit a duplicate unique ID if a bespoke entity class ever picks a
#     key the generic loop also builds.
#
# So a merge can be correct for the seven and still quietly delete or rename a dozen
# others. LIVE_REGISTRY is the reference installation's 134 rows as they stood before
# the v1.8.14 merge (one config entry, device name "Growatt", profile
# wit_4000_15000tl3). The tests below replay setup against it and require every deletion
# and every rename to be one somebody decided on.
#
# When a deliberate change lands, add the row to EXPECTED_REMOVALS with the reason.
# Regenerating the snapshot instead defeats the point of having it.
# ---------------------------------------------------------------------------

LIVE_REGISTRY: dict[tuple[str, str], str] = {
    # --- binary_sensor ---
    ("binary_sensor", "inverter_online"): "binary_sensor.growatt_inverter_online",
    # --- number ---
    ("number", "active_power_rate"): "number.growatt_vpp_active_power_rate",
    ("number", "active_power_rate_vpp"): "number.growatt_active_power_rate_vpp",
    ("number", "remote_charge_and_discharge_power"): "number.growatt_battery_remote_charge_and_discharge_power",
    ("number", "remote_power_control_charging_time"): "number.growatt_grid_remote_power_control_charging_time",
    ("number", "vpp_charge_cutoff_soc"): "number.growatt_battery_vpp_charge_cutoff_soc",
    ("number", "vpp_discharge_cutoff_soc"): "number.growatt_battery_vpp_discharge_cutoff_soc",
    ("number", "vpp_export_limit_power_rate"): "number.growatt_grid_vpp_export_limit_power_rate",
    ("number", "vpp_power_percent"): "number.growatt_vpp_power_rate",
    ("number", "vpp_tou_p10_power"): "number.growatt_tou_period_10_power",
    ("number", "vpp_tou_p1_power"): "number.growatt_tou_period_1_power",
    ("number", "vpp_tou_p2_power"): "number.growatt_tou_period_2_power",
    ("number", "vpp_tou_p3_power"): "number.growatt_tou_period_3_power",
    ("number", "vpp_tou_p4_power"): "number.growatt_tou_period_4_power",
    ("number", "vpp_tou_p5_power"): "number.growatt_tou_period_5_power",
    ("number", "vpp_tou_p6_power"): "number.growatt_tou_period_6_power",
    ("number", "vpp_tou_p7_power"): "number.growatt_tou_period_7_power",
    ("number", "vpp_tou_p8_power"): "number.growatt_tou_period_8_power",
    ("number", "vpp_tou_p9_power"): "number.growatt_tou_period_9_power",
    ("number", "vpp_tou_periods"): "number.growatt_tou_active_periods",
    # --- select ---
    ("select", "control_authority"): "select.growatt_grid_control_authority",
    ("select", "remote_power_control_enable"): "select.growatt_grid_remote_power_control_enable",
    ("select", "vpp_battery_mode"): "select.growatt_battery_mode_vpp",
    ("select", "vpp_export_limit_enable"): "select.growatt_grid_vpp_export_limit_enable",
    ("select", "vpp_tou_default_mode"): "select.growatt_tou_default_mode",
    ("select", "wit_mode_preset"): "select.growatt_mode_preset",
    ("select", "work_mode"): "select.growatt_work_mode",
    # --- sensor ---
    ("sensor", "ac_charge_energy_today"): "sensor.growatt_battery_ac_charge_energy_today",
    ("sensor", "ac_charge_energy_total"): "sensor.growatt_battery_ac_charge_energy_total",
    ("sensor", "ac_current"): "sensor.growatt_solar_ac_current",
    ("sensor", "ac_current_r"): "sensor.growatt_solar_ac_current_phase_r",
    ("sensor", "ac_current_s"): "sensor.growatt_solar_ac_current_phase_s",
    ("sensor", "ac_current_t"): "sensor.growatt_solar_ac_current_phase_t",
    ("sensor", "ac_discharge_energy_total"): "sensor.growatt_battery_ac_discharge_energy_total",
    ("sensor", "ac_frequency"): "sensor.growatt_solar_ac_frequency",
    ("sensor", "ac_power"): "sensor.growatt_solar_ac_power",
    ("sensor", "ac_power_r"): "sensor.growatt_solar_ac_power_phase_r",
    ("sensor", "ac_power_s"): "sensor.growatt_solar_ac_power_phase_s",
    ("sensor", "ac_power_t"): "sensor.growatt_solar_ac_power_phase_t",
    ("sensor", "ac_voltage"): "sensor.growatt_load_ac_voltage",
    ("sensor", "ac_voltage_r"): "sensor.growatt_solar_ac_voltage_r",
    ("sensor", "ac_voltage_rs"): "sensor.growatt_solar_ac_voltage_rs",
    ("sensor", "ac_voltage_s"): "sensor.growatt_solar_ac_voltage_s",
    ("sensor", "ac_voltage_st"): "sensor.growatt_solar_ac_voltage_st",
    ("sensor", "ac_voltage_t"): "sensor.growatt_solar_ac_voltage_t",
    ("sensor", "ac_voltage_tr"): "sensor.growatt_solar_ac_voltage_tr",
    ("sensor", "battery_charge_power"): "sensor.growatt_battery_battery_charge_power",
    ("sensor", "battery_charge_today"): "sensor.growatt_battery_battery_charge_today",
    ("sensor", "battery_charge_total"): "sensor.growatt_battery_battery_charge_total",
    ("sensor", "battery_current"): "sensor.growatt_battery_battery_current",
    ("sensor", "battery_discharge_power"): "sensor.growatt_battery_battery_discharge_power",
    ("sensor", "battery_discharge_today"): "sensor.growatt_battery_battery_discharge_today",
    ("sensor", "battery_discharge_total"): "sensor.growatt_battery_battery_discharge_total",
    ("sensor", "battery_power"): "sensor.growatt_battery_battery_power",
    ("sensor", "battery_soc"): "sensor.growatt_battery_battery_soc",
    ("sensor", "battery_soh"): "sensor.growatt_battery_battery_state_of_health",
    ("sensor", "battery_temp"): "sensor.growatt_battery_battery_temperature",
    ("sensor", "battery_voltage"): "sensor.growatt_battery_battery_voltage",
    ("sensor", "battery_voltage_bms"): "sensor.growatt_battery_battery_voltage_bms",
    ("sensor", "boost_temp"): "sensor.growatt_boost_temperature",
    ("sensor", "derating_mode"): "sensor.growatt_derating_mode",
    ("sensor", "dry_contact_state"): "sensor.growatt_dry_contact_state",
    ("sensor", "enable_spec_set"): "sensor.growatt_appointed_spec_setting",
    ("sensor", "energy_to_grid_today"): "sensor.growatt_grid_energy_to_grid_today",
    ("sensor", "energy_to_grid_total"): "sensor.growatt_grid_energy_to_grid_total",
    ("sensor", "energy_to_user_today"): "sensor.growatt_energy_to_user_today",
    ("sensor", "energy_to_user_total"): "sensor.growatt_energy_to_user_total",
    ("sensor", "energy_today"): "sensor.growatt_solar_energy_today",
    ("sensor", "energy_total"): "sensor.growatt_solar_energy_total",
    ("sensor", "extra_energy_today"): "sensor.growatt_solar_extra_energy_today",
    ("sensor", "extra_energy_total"): "sensor.growatt_solar_extra_energy_total",
    ("sensor", "extra_power_to_grid"): "sensor.growatt_grid_extra_power_to_grid",
    ("sensor", "fast_mppt_enable"): "sensor.growatt_fast_mppt_enable",
    ("sensor", "fault_code"): "sensor.growatt_fault_code",
    ("sensor", "grid_connection_status"): "sensor.growatt_grid_grid_connection_status",
    ("sensor", "grid_energy_today"): "sensor.growatt_grid_grid_energy_today",
    ("sensor", "grid_energy_total"): "sensor.growatt_grid_grid_energy_total",
    ("sensor", "grid_export_power"): "sensor.growatt_grid_grid_export_power",
    ("sensor", "grid_import_energy_today"): "sensor.growatt_grid_grid_import_energy_today",
    ("sensor", "grid_import_energy_total"): "sensor.growatt_grid_grid_import_energy_total",
    ("sensor", "grid_import_power"): "sensor.growatt_grid_grid_import_power",
    ("sensor", "grid_power"): "sensor.growatt_grid_grid_power",
    ("sensor", "house_consumption"): "sensor.growatt_load_house_consumption",
    ("sensor", "inverter_temp"): "sensor.growatt_inverter_temperature",
    ("sensor", "ipm_temp"): "sensor.growatt_ipm_temperature",
    ("sensor", "last_update"): "sensor.growatt_last_update",
    ("sensor", "load_energy_today"): "sensor.growatt_load_load_energy_today",
    ("sensor", "load_energy_total"): "sensor.growatt_load_load_energy_total",
    ("sensor", "nonstd_vac_enable"): "sensor.growatt_non_standard_vac_enable",
    ("sensor", "ntognd_detect"): "sensor.growatt_ntognd_detect",
    ("sensor", "power_to_grid"): "sensor.growatt_grid_power_to_grid",
    ("sensor", "power_to_load"): "sensor.growatt_load_power_to_load",
    ("sensor", "power_to_user"): "sensor.growatt_load_power_to_user",
    ("sensor", "priority_mode"): "sensor.growatt_battery_priority_mode",
    ("sensor", "pv1_current"): "sensor.growatt_solar_pv1_current",
    ("sensor", "pv1_energy_today"): "sensor.growatt_solar_pv1_energy_today",
    ("sensor", "pv1_energy_total"): "sensor.growatt_solar_pv1_energy_total",
    ("sensor", "pv1_power"): "sensor.growatt_solar_pv1_power",
    ("sensor", "pv1_voltage"): "sensor.growatt_solar_pv1_voltage",
    ("sensor", "pv2_current"): "sensor.growatt_solar_pv2_current",
    ("sensor", "pv2_energy_today"): "sensor.growatt_solar_pv2_energy_today",
    ("sensor", "pv2_energy_total"): "sensor.growatt_solar_pv2_energy_total",
    ("sensor", "pv2_power"): "sensor.growatt_solar_pv2_power",
    ("sensor", "pv2_voltage"): "sensor.growatt_solar_pv2_voltage",
    ("sensor", "pv_energy_total"): "sensor.growatt_solar_pv_energy_total",
    ("sensor", "pv_total_power"): "sensor.growatt_solar_solar_total_power",
    ("sensor", "self_consumption"): "sensor.growatt_load_self_consumption",
    ("sensor", "self_consumption_percentage"): "sensor.growatt_solar_self_consumption_percentage",
    ("sensor", "status"): "sensor.growatt_status",
    ("sensor", "system_output_power"): "sensor.growatt_solar_system_output_power",
    ("sensor", "warning_code"): "sensor.growatt_warning_code",
    ("sensor", "wit_mode_status"): "sensor.growatt_inverter_mode",
    # --- switch ---
    ("switch", "battery_optimizer_switch"): "switch.growatt_battery_optimizer",
    ("switch", "grid_export_switch"): "switch.growatt_grid_export",
    # --- time ---
    ("time", "vpp_tou_p10_end"): "time.growatt_battery_growatt_tou_period_10_end",
    ("time", "vpp_tou_p10_start"): "time.growatt_battery_growatt_tou_period_10_start",
    ("time", "vpp_tou_p1_end"): "time.growatt_battery_growatt_tou_period_1_end",
    ("time", "vpp_tou_p1_start"): "time.growatt_battery_growatt_tou_period_1_start",
    ("time", "vpp_tou_p2_end"): "time.growatt_battery_growatt_tou_period_2_end",
    ("time", "vpp_tou_p2_start"): "time.growatt_battery_growatt_tou_period_2_start",
    ("time", "vpp_tou_p3_end"): "time.growatt_battery_growatt_tou_period_3_end",
    ("time", "vpp_tou_p3_start"): "time.growatt_battery_growatt_tou_period_3_start",
    ("time", "vpp_tou_p4_end"): "time.growatt_battery_growatt_tou_period_4_end",
    ("time", "vpp_tou_p4_start"): "time.growatt_battery_growatt_tou_period_4_start",
    ("time", "vpp_tou_p5_end"): "time.growatt_battery_growatt_tou_period_5_end",
    ("time", "vpp_tou_p5_start"): "time.growatt_battery_growatt_tou_period_5_start",
    ("time", "vpp_tou_p6_end"): "time.growatt_battery_growatt_tou_period_6_end",
    ("time", "vpp_tou_p6_start"): "time.growatt_battery_growatt_tou_period_6_start",
    ("time", "vpp_tou_p7_end"): "time.growatt_battery_growatt_tou_period_7_end",
    ("time", "vpp_tou_p7_start"): "time.growatt_battery_growatt_tou_period_7_start",
    ("time", "vpp_tou_p8_end"): "time.growatt_battery_growatt_tou_period_8_end",
    ("time", "vpp_tou_p8_start"): "time.growatt_battery_growatt_tou_period_8_start",
    ("time", "vpp_tou_p9_end"): "time.growatt_battery_growatt_tou_period_9_end",
    ("time", "vpp_tou_p9_start"): "time.growatt_battery_growatt_tou_period_9_start",
}

assert len(LIVE_REGISTRY) == 134, "the snapshot is 134 rows; do not edit it casually"

# Rows the merge deletes on purpose. Both were decided before this test existed; the
# entry exists so the deletion stays a decision rather than becoming a surprise.
EXPECTED_REMOVALS = {
    ("sensor", "ac_discharge_energy_total"): (
        "upstream v1.8.14 dropped it from the WIT battery group - protocol V1.39 has no "
        "AC-discharge counter. Documented in RELEASENOTES.md; battery_optimizer reads "
        "battery_discharge_today instead."
    ),
    ("number", "active_power_rate"): (
        "on WIT the bespoke GrowattWitActivePowerRateNumber "
        "({entry}_active_power_rate_vpp) supersedes it, and the generic loop that built "
        "this row is never reached. The blanket control rule cannot see it - register 201 "
        "is present and writable - so __init__ removes it by name."
    ),
}

# Rows nothing in the code can build, and nothing in the code can remove either: they
# have no SENSOR_DEFINITIONS entry, so the blanket sensor rule (which iterates that dict)
# never reaches them. Pre-existing - identical in the fork and in upstream v1.8.14 - and
# recorded here so the count below stays honest rather than being papered over.
KNOWN_ORPHANS = {
    ("sensor", "energy_to_user_today"),
    ("sensor", "energy_to_user_total"),
}


def _install_remaining_platform_stubs() -> None:
    """Stubs for the six platforms beyond sensor, so setup can actually be run.

    Same conditions as _install_sensor_platform_stubs: skipped when a real Home Assistant
    is installed, and every value is an inert placeholder that only has to be
    constructible.
    """
    ha = sys.modules.get("homeassistant")
    if ha is None or getattr(ha, "__file__", None):  # pragma: no cover - no HA here
        return

    components = sys.modules["homeassistant.components"]

    class _EntityBase:  # noqa: D401 - stand-in base
        """Placeholder; the platforms only set _attr_* on it."""

        _attr_should_poll = False

    for mod_name, class_names in (
        ("number", ("NumberEntity",)),
        ("select", ("SelectEntity",)),
        ("time", ("TimeEntity",)),
        ("switch", ("SwitchEntity",)),
        ("button", ("ButtonEntity",)),
        ("binary_sensor", ("BinarySensorEntity",)),
    ):
        full = f"homeassistant.components.{mod_name}"
        if full in sys.modules:
            continue
        mod = types.ModuleType(full)
        for cls_name in class_names:
            setattr(mod, cls_name, type(cls_name, (_EntityBase,), {}))
        setattr(components, mod_name, mod)
        sys.modules[full] = mod

    sys.modules["homeassistant.components.number"].NumberMode = _namespace("NumberMode")
    sys.modules["homeassistant.components.binary_sensor"].BinarySensorDeviceClass = (
        _namespace("BinarySensorDeviceClass")
    )

    helpers = sys.modules["homeassistant.helpers"]
    if "homeassistant.helpers.entity_registry" not in sys.modules:
        registry_mod = types.ModuleType("homeassistant.helpers.entity_registry")
        registry_mod.async_get = lambda hass: None
        helpers.entity_registry = registry_mod
        sys.modules["homeassistant.helpers.entity_registry"] = registry_mod

    util = sys.modules["homeassistant.util"]
    if not hasattr(util, "slugify"):
        util.slugify = _slugify


_install_remaining_platform_stubs()

PLATFORMS = ("sensor", "binary_sensor", "number", "select", "time", "switch", "button")

ENTRY_ID = "01KBB0DFK8WSEB83341HYNM1MX"
REGISTER_MAP = "WIT_4000_15000TL3"


class _LiveCoordinator:
    """Just enough coordinator for the seven platform setups.

    `data` is a default GrowattData, which is exactly what
    `async_config_entry_first_refresh()` seeds: no poll has happened by the time the
    platforms are forwarded.
    """

    def __init__(self, growatt_modbus_mod) -> None:
        self.data = growatt_modbus_mod.GrowattData()
        self.modbus_client = type(
            "_Client",
            (),
            {
                "is_clock_supported": True,
                "register_map": _const.REGISTER_MAPS[REGISTER_MAP],
            },
        )()
        self.last_update_success = True
        self.is_online = True
        self.has_real_data = False
        self.wit_mode_preset_last = None
        self.listeners = []

    def get_device_info(self, device_type):
        return {"identifiers": {("growatt_modbus", f"{ENTRY_ID}_{device_type}")}}

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: None

    def poll_arrives(self, growatt_modbus_mod) -> None:
        """Publish a fully-populated GrowattData and notify the listeners.

        This is the state the deferred-sensor listener in sensor.py waits for. Every
        numeric field gets a non-zero value and every empty string a non-empty one, so
        that every `condition` in SENSOR_DEFINITIONS which can pass does pass. Strings
        matter as much as numbers here: `wit_mode_status` is a dataclass field defaulting
        to `""`, and its condition tests the VALUE precisely because `hasattr` never
        could - leaving it empty would hide the duplicate this exists to catch.

        The two attributes that are NOT dataclass fields (`battery_soh`,
        `battery_voltage_bms` - see the comment above GrowattData) are set too.
        """
        data = growatt_modbus_mod.GrowattData()
        for field in data.__dataclass_fields__:
            value = getattr(data, field)
            if isinstance(value, bool):
                continue
            if isinstance(value, float):
                setattr(data, field, 1.0)
            elif isinstance(value, int):
                setattr(data, field, 1)
            elif isinstance(value, str) and not value:
                setattr(data, field, "set")
        data.serial_number = "SNAPSHOT"
        # What coordinator._compute_wit_mode_status writes on a WIT poll.
        data.wit_mode_status = "Passthrough"
        data.battery_soh = 100.0
        data.battery_voltage_bms = 51.2
        self.data = data
        self.has_real_data = True
        for callback in list(self.listeners):
            callback()


class _LiveEntry:
    """The reference installation's config entry, verbatim from its stored data."""

    entry_id = ENTRY_ID
    title = DEVICE_NAME

    def __init__(self, coordinator) -> None:
        self.data = {
            "device_structure_version": 2,
            "host": "192.168.33.8",
            "inverter_series": PROFILE,
            "name": DEVICE_NAME,
            "port": 502,
            "register_map": REGISTER_MAP,
            "slave_id": 1,
        }
        self.options = {"device_name": DEVICE_NAME, "inverter_series": PROFILE}
        self.runtime_data = coordinator

    def async_on_unload(self, func):
        return func


def _build_entities(*, after_first_poll: bool = False) -> dict:
    """Run every platform's async_setup_entry; return {domain: [(unique_key, class)]}.

    With `after_first_poll`, a populated poll result is published afterwards and the
    coordinator listeners are fired, so the deferred sensors sensor.py registers are
    included too. Entities added then are appended to the same list, deliberately not
    de-duplicated: a key appearing twice is the failure being looked for.
    """
    import asyncio

    growatt_modbus = importlib.import_module("growatt_under_test.growatt_modbus")
    coordinator = _LiveCoordinator(growatt_modbus)
    entry = _LiveEntry(coordinator)
    hass = type("_Hass", (), {"data": {}})()

    built = {}
    collectors = {}
    for domain in PLATFORMS:
        platform = importlib.import_module(f"growatt_under_test.{domain}")
        collected = []
        collectors[domain] = collected
        asyncio.run(
            platform.async_setup_entry(
                # `sink=collected` binds the current list. Without it the closure would
                # capture the loop variable, and every entity the deferred listener adds
                # after the loop has finished would land in the last platform's list.
                hass, entry, lambda ents, *a, sink=collected, **k: sink.extend(ents)
            )
        )

    if after_first_poll:
        coordinator.poll_arrives(growatt_modbus)

    for domain, collected in collectors.items():
        built[domain] = [
            (e._attr_unique_id[len(ENTRY_ID) + 1:], type(e).__name__) for e in collected
        ]
    return built


def _profile_holding_registers() -> dict:
    profile = _device_profiles.get_profile(PROFILE)
    return _const.REGISTER_MAPS[profile["register_map"]].get("holding_registers", {})


def _simulate_removals() -> dict:
    """Replay every registry removal __init__.async_setup_entry performs, in order."""
    removed = {}

    def remove(domain: str, key: str, why: str) -> None:
        if (domain, key) in LIVE_REGISTRY:
            removed.setdefault((domain, key), why)

    writable = _const.WRITABLE_REGISTERS

    for name in {
        k for k in writable if "time_period" in k and k.endswith(("_start", "_end"))
    }:
        remove("number", name, "time_period start/end migrated to a time entity")
    for period in range(1, 11):
        for slot in ("start", "end"):
            remove(
                "number",
                f"vpp_tou_p{period}_{slot}",
                "WIT TOU start/end migrated to a time entity",
            )
    remove("number", "export_limit_w", "register 203 is not writable on WIT")
    if REGISTER_MAP in _const.WIT_REGISTER_MAPS:
        remove(
            "number", "active_power_rate", "superseded on WIT by active_power_rate_vpp"
        )

    holding = _profile_holding_registers()
    holding_names = {r.get("name") for r in holding.values() if isinstance(r, dict)}
    for name in ("discharge_stopped_soc", "charge_stopped_soc"):
        if name not in holding_names:
            remove("number", name, "SOC-limit register not in this profile")

    profile = _device_profiles.get_profile(PROFILE)
    inputs = _const.REGISTER_MAPS[profile["register_map"]].get("input_registers", {})
    input_names = {r.get("name") for r in inputs.values() if isinstance(r, dict)}
    if "battery_temp" not in input_names:
        remove("sensor", "battery_temp", "battery_temp is not an input register here")

    for key in SENSOR_DEFINITIONS:
        if key not in WIT_SENSORS:
            remove("sensor", key, "blanket rule: key is not in the profile sensor set")

    for name, config in _const.WRITABLE_REGISTERS.items():
        register = config.get("register")
        if register in holding and not _const.is_read_only_register(
            holding.get(register)
        ):
            continue
        for domain in ("number", "select", "time"):
            remove(
                domain, name, f"blanket rule: register {register} absent or read-only"
            )

    return removed


def _simulate_renames() -> dict:
    """Replay __init__._migrate_entity_ids against the snapshot."""
    renamed = {}

    def check(key, target):
        current = LIVE_REGISTRY[key]
        if current != target:
            renamed[key] = (current, target)

    for sensor_key in SENSOR_DEFINITIONS:
        if ("sensor", sensor_key) in LIVE_REGISTRY:
            check(("sensor", sensor_key), _migrated_entity_id(sensor_key))

    if ("binary_sensor", "inverter_online") in LIVE_REGISTRY:
        check(
            ("binary_sensor", "inverter_online"),
            f"binary_sensor.{_slugify(DEVICE_NAME)}_inverter_online",
        )

    # Verbatim copy of __init__._NUMBER_FRIENDLY_OVERRIDES. That dict is already
    # documented there as a duplicate of number.py's which has drifted; restating it here
    # is what lets this test show whether the drift reaches a live row (it does not).
    number_overrides = {
        "active_power_rate": "VPP Active Power Rate",
        "export_limit_w": "VPP Export Limit (W)",
        "max_output_power_rate": "Max Output Power Rate",
        "vpp_export_limit_power_rate": "VPP Export Limit Power Rate",
        "grid_charge_stopped_soc": "Grid Charge Stopped SOC",
    }
    suffixes = {"solar": "Solar", "grid": "Grid", "load": "Load", "battery": "Battery"}
    for control_name in _const.WRITABLE_REGISTERS:
        base = control_name.replace("_", " ").title()
        for domain in ("select", "number", "time"):
            if (domain, control_name) not in LIVE_REGISTRY:
                continue
            friendly = (
                number_overrides.get(control_name, base) if domain == "number" else base
            )
            suffix = suffixes.get(_const.get_device_type_for_control(control_name))
            device_name = f"{DEVICE_NAME} {suffix}" if suffix else DEVICE_NAME
            check(
                (domain, control_name),
                f"{domain}.{_slugify(device_name)}_{_slugify(friendly)}",
            )

    return renamed


def test_only_documented_rows_are_removed() -> None:
    """Every deletion the merged setup performs has to be one somebody chose.

    A removal is permanent and silent: the row goes, and with it the entity's name, its
    area, every dashboard and automation reference, and its long-term statistics link.
    """
    removed = _simulate_removals()
    unexpected = {k: v for k, v in removed.items() if k not in EXPECTED_REMOVALS}
    assert not unexpected, (
        "setup would delete registry rows nobody signed off on: "
        + "; ".join(f"{LIVE_REGISTRY[k]} ({v})" for k, v in sorted(unexpected.items()))
    )
    missing = set(EXPECTED_REMOVALS) - set(removed)
    assert not missing, (
        f"EXPECTED_REMOVALS lists rows that are no longer removed: {sorted(missing)} - "
        f"drop them from the map rather than leaving it stale"
    )


def test_no_live_row_is_renamed() -> None:
    """_migrate_entity_ids runs on every setup and must be a no-op for this install.

    A rename does not break the entity, it breaks everything that names it: the seven
    IDs configured in battery_optimizer, plus dashboards, automations and statistics.
    """
    renamed = _simulate_renames()
    assert not renamed, "setup would rename live entities: " + "; ".join(
        f"{old} -> {new}" for old, new in sorted(renamed.values())
    )


def test_no_unique_id_is_produced_twice() -> None:
    """Two entities with one unique ID: Home Assistant keeps the first and drops the rest.

    The realistic way to get there is a bespoke entity class picking a key the generic
    loop also builds - `wit_mode_status` is exactly that shape - or a second definition
    table being wired in: `battery_sensors.py` sits in the same package and redefines
    `battery_soc`, `battery_temp` and four more under different names. It is unimported
    today (tests/test_module_globals.py records that), and this test is what would notice
    if it stopped being.
    """
    from collections import Counter

    # Both phases matter, and the second is the dangerous one: at setup time most
    # condition-gated sensors are skipped, so a key the generic loop should not own
    # looks harmless. The duplicate only lands when the first real poll arrives and the
    # deferred listener builds what it was holding.
    for after_first_poll in (False, True):
        for domain, entities in _build_entities(
            after_first_poll=after_first_poll
        ).items():
            counts = Counter(key for key, _cls in entities)
            duplicates = {
                key: [cls for k, cls in entities if k == key]
                for key, n in counts.items()
                if n > 1
            }
            assert not duplicates, (
                f"{domain}: duplicate unique_id suffixes {duplicates} "
                f"(after_first_poll={after_first_poll})"
            )


def test_the_seven_are_built_exactly_once() -> None:
    """End to end: run the sensor platform and look for the seven, not for their inputs."""
    sensors = dict(_build_entities()["sensor"])
    for key in OPTIMIZER_SENSORS:
        assert key in sensors, (
            f"{key} was not built by sensor.async_setup_entry, so "
            f"{OPTIMIZER_SENSORS[key][0]} does not exist"
        )
    assert sensors["wit_mode_status"] == "GrowattWitModeStatusSensor"


def test_every_surviving_row_still_has_a_producer() -> None:
    """A row nothing builds and nothing removes lingers forever as `unavailable`.

    Sensors are checked against the profile intersection rather than against one setup
    run, because condition-gated sensors are added later by the deferred listener -
    `battery_soh` and `battery_voltage_bms` are not dataclass fields at all until their
    register answers, so they cannot be created at setup time by design.
    """
    built = _build_entities()
    producible = {"sensor": (set(SENSOR_DEFINITIONS) & WIT_SENSORS) | {"inverter_clock"}}
    for domain in PLATFORMS:
        producible.setdefault(domain, {key for key, _cls in built[domain]})

    removed = set(_simulate_removals())
    orphans = {
        (domain, key)
        for (domain, key) in LIVE_REGISTRY
        if (domain, key) not in removed and key not in producible[domain]
    }
    unexpected = orphans - KNOWN_ORPHANS
    assert not unexpected, (
        "these live rows can neither be recreated nor removed, so they stay unavailable "
        "forever: "
        + ", ".join(f"{LIVE_REGISTRY[k]} <{k[1]}>" for k in sorted(unexpected))
    )
    assert KNOWN_ORPHANS <= orphans, (
        f"KNOWN_ORPHANS is stale - these are producible again: "
        f"{sorted(KNOWN_ORPHANS - orphans)}"
    )
