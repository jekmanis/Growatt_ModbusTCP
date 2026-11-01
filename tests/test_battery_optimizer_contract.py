"""What the battery optimizer consumes from this integration, in one place.

The optimizer is a Home Assistant add-on (C:\\darbs\\battery-optimizer, branch master). It
couples to growatt_modbus through exactly these surfaces, and this file pins each of them
from the optimizer's side - the values below are copied from its source, with the file
each came from, so a change on either side fails here first:

  (a) action `growatt_modbus.set_wit_mode` - its whole write path
      (app/battery_optimizer_lib/direct_control.py, DirectControl._apply_locked)
  (b) action `growatt_modbus.get_register_data` - its verify-after-set read of holding
      30407+4 and 30200+2 (direct_control.py RegisterVerifier._read_block, config.py
      `verify_source`)
  (c) seven sensor entity ids (addon/battery_optimizer/options.example.yaml,
      direct_control.DEFAULT_MODE_STATUS_ENTITY)
  (d) the exact Inverter Mode strings (direct_control.MODE_STATUS_MAP and
      decode_wit_mode_status, a port of _compute_wit_mode_status)

Deeper checks live next door and are not repeated here: tests/test_wit_service_contract.py
runs both handlers against an in-memory register bank (registers written per mode, the
response envelope, bus timeouts), and tests/test_wit_entity_keys.py replays entity setup
against the live 134-row registry. This file is the summary those two prove in detail,
plus the parts neither pins: the exact parameter ranges, the exact get_register_data
schema and return shapes, and the mode strings compared against the optimizer's decoder.

If the optimizer changes what it sends or reads, update the constants here in the same
change; if this file fails after an integration change, the optimizer needs adapting.
"""
from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_wit_entity_keys as entity_keys  # noqa: E402

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
DIAGNOSTIC_SRC = (COMPONENT / "diagnostic.py").read_text(encoding="utf-8")
DIAGNOSTIC_TREE = ast.parse(DIAGNOSTIC_SRC)

# ---------------------------------------------------------------------------
# The optimizer's side, copied from its source
# ---------------------------------------------------------------------------

# direct_control.MODE_STATUS_MAP: every mode string DirectControl sends, and the Inverter
# Mode status it expects to read back for it. It sends "hold", never "preserve_soc", but
# keeps both keys.
OPTIMIZER_MODE_STATUS = {
    "grid_charge": "Grid Charge",
    "hold": "Preserve SOC",
    "preserve_soc": "Preserve SOC",
    "max_export": "Max Export",
    "discharge_to_grid": "Discharge to Grid",
    "discharge_to_load": "Discharge to Load",
    "passthrough": "Passthrough",
}

# direct_control.AC_CHARGE_MODE_VALUES - compared numerically against 30410.
OPTIMIZER_AC_CHARGE_MODES = {"disabled": 0, "pv_priority": 1, "ac_priority": 2}

# The set_wit_mode parameters DirectControl can put in a call (_apply_locked, plus
# release_control(), which sends only device_id and mode).
OPTIMIZER_SET_WIT_MODE_PARAMS = {
    "device_id", "mode", "duration_minutes", "power_percent", "export_rate",
    "ac_charge_mode", "charge_cutoff_soc", "discharge_cutoff_soc",
}

# RegisterVerifier: REG_BLOCK_CONTROL and REG_BLOCK_EXPORT, both register_type="holding".
OPTIMIZER_READ_BLOCKS = ((30407, 4), (30200, 2))

# options.example.yaml (soc_sensor, pv_power_sensor, battery_temp_sensor,
# battery_charge_sensor, battery_discharge_sensor, load_power_sensor) and
# direct_control.DEFAULT_MODE_STATUS_ENTITY (inverter_mode_sensor). Keyed by the
# integration's sensor key.
OPTIMIZER_ENTITY_IDS = {
    "battery_soc": "sensor.growatt_battery_battery_soc",
    "pv_total_power": "sensor.growatt_solar_solar_total_power",
    "battery_temp": "sensor.growatt_battery_battery_temperature",
    "battery_charge_today": "sensor.growatt_battery_battery_charge_today",
    "battery_discharge_today": "sensor.growatt_battery_battery_discharge_today",
    "house_consumption": "sensor.growatt_load_house_consumption",
    "wit_mode_status": "sensor.growatt_inverter_mode",
}


def optimizer_decode_wit_mode_status(regs: dict) -> str:
    """Verbatim port of direct_control.decode_wit_mode_status (raw u16 registers in)."""
    remote_enable = int(regs.get(30407, 0) or 0)
    raw = int(regs.get(30409, 0) or 0)
    power_signed = raw - 65536 if raw > 32767 else raw
    export_enable = int(regs.get(30200, 0) or 0)
    export_rate = int(regs.get(30201, 0) or 0)

    if not remote_enable:
        return "Passthrough"
    if power_signed > 1:
        return "Grid Charge"
    if power_signed in (0, 1):
        return "Preserve SOC"
    if power_signed < 0:
        export_allowed = (not export_enable) or (export_rate > 0)
        if export_allowed and abs(power_signed) == 100:
            return "Max Export"
        if export_allowed:
            return "Discharge to Grid"
        return "Discharge to Load"
    return "Passthrough"


# ---------------------------------------------------------------------------
# Reading a vol.Schema out of diagnostic.py
# ---------------------------------------------------------------------------
#
# voluptuous is stubbed in this suite, so the schema objects carry no structure. The AST
# does, and describing each validator semantically (range, choices, default) rather than
# comparing source text keeps a harmless reformat from failing the contract.


def _module_assignment(name: str) -> ast.AST:
    for node in DIAGNOSTIC_TREE.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"diagnostic.{name} is no longer a module-level assignment")


def _resolve_choices(node: ast.AST, const) -> list:
    if isinstance(node, ast.Name):
        target = _module_assignment(node.id)
        if isinstance(target, ast.Name):  # an alias such as WIT_MODE_CHOICES = WIT_MODES
            return list(getattr(const, target.id))
        return ast.literal_eval(target)
    return ast.literal_eval(node)


def _describe(node: ast.AST, const) -> dict:
    """{'type': ..., 'range': (min, max), 'choices': [...]} for one validator."""
    out: dict = {}
    if isinstance(node, ast.Attribute):  # cv.string
        out["type"] = node.attr
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        kind = node.func.attr
        if kind == "All":
            for arg in node.args:
                out.update(_describe(arg, const))
        elif kind == "Coerce":
            out["type"] = ast.unparse(node.args[0])
        elif kind == "Range":
            kw = {k.arg: ast.literal_eval(k.value) for k in node.keywords}
            out["range"] = (kw.get("min"), kw.get("max"))
        elif kind == "In":
            out["choices"] = _resolve_choices(node.args[0], const)
        else:
            out[kind] = ast.unparse(node)
    elif isinstance(node, ast.Name):  # a plain callable such as _battery_mode_alias
        out.setdefault("pre", []).append(node.id)
    else:
        out["other"] = ast.unparse(node)
    return out


def _schema(name: str, const) -> dict:
    """{key: {'required': bool, 'default': ..., <validator description>}}."""
    call = _module_assignment(name)
    assert isinstance(call, ast.Call) and ast.unparse(call.func) == "vol.Schema"
    fields = {}
    for key_node, value_node in zip(call.args[0].keys, call.args[0].values):
        marker = key_node.func.attr  # Required / Optional
        key = ast.literal_eval(key_node.args[0])
        entry = {"required": marker == "Required"}
        for kw in key_node.keywords:
            if kw.arg == "default":
                entry["default"] = ast.literal_eval(kw.value)
        entry.update(_describe(value_node, const))
        fields[key] = entry
    return fields


# ---------------------------------------------------------------------------
# (a) set_wit_mode
# ---------------------------------------------------------------------------

# The schema as the optimizer relies on it. Ranges are part of the contract: voluptuous
# rejects an out-of-range value outright, so narrowing one fails every call that used it.
EXPECTED_SET_WIT_MODE_SCHEMA = {
    "device_id": {"required": True, "type": "string"},
    "mode": {"required": True},  # choices checked separately against the optimizer's modes
    "power_percent": {"required": False, "default": 100, "type": "int", "range": (1, 100)},
    "duration_minutes": {"required": False, "default": 60, "type": "int", "range": (1, 1440)},
    "export_rate": {"required": False, "type": "int", "range": (0, 100)},
    "ac_charge_mode": {"required": False,
                       "choices": ["disabled", "pv_priority", "ac_priority"]},
    "charge_cutoff_soc": {"required": False, "type": "int", "range": (70, 100)},
    "discharge_cutoff_soc": {"required": False, "type": "int", "range": (10, 30)},
}


def test_set_wit_mode_schema_is_exactly_what_the_optimizer_relies_on(component_const):
    schema = _schema("SERVICE_SET_WIT_MODE_SCHEMA", component_const)
    assert set(schema) == OPTIMIZER_SET_WIT_MODE_PARAMS, (
        "set_wit_mode's parameters changed: a removed one fails every optimizer call "
        "that sends it ('extra keys not allowed'), a new required one fails them all"
    )
    for key, expected in EXPECTED_SET_WIT_MODE_SCHEMA.items():
        actual = {k: v for k, v in schema[key].items() if k != "choices" or key != "mode"}
        assert actual == expected, f"set_wit_mode.{key}: {actual} != {expected}"


def test_every_mode_the_optimizer_sends_is_accepted_including_hold(component_const):
    modes = _schema("SERVICE_SET_WIT_MODE_SCHEMA", component_const)["mode"]["choices"]
    missing = set(OPTIMIZER_MODE_STATUS) - set(modes)
    assert not missing, f"set_wit_mode no longer accepts {sorted(missing)}"
    assert "hold" in modes, "the optimizer's HOLD slots send 'hold'"


def test_the_ac_charge_mode_values_are_the_optimizers(component_const):
    assert component_const.WIT_AC_CHARGE_MODES == OPTIMIZER_AC_CHARGE_MODES


# ---------------------------------------------------------------------------
# (b) get_register_data
# ---------------------------------------------------------------------------

EXPECTED_GET_REGISTER_DATA_SCHEMA = {
    "device_id": {"required": True, "type": "string"},
    "register_type": {"required": False, "default": "input",
                      "choices": ["input", "holding"]},
    "start_address": {"required": True, "type": "int", "range": (0, 65535)},
    "count": {"required": True, "type": "int", "range": (1, 50)},
}


def test_get_register_data_schema_is_exactly_what_the_optimizer_sends(component_const):
    schema = _schema("SERVICE_GET_REGISTER_DATA_SCHEMA", component_const)
    assert schema == EXPECTED_GET_REGISTER_DATA_SCHEMA
    low, high = schema["count"]["range"]
    for start, count in OPTIMIZER_READ_BLOCKS:
        assert low <= count <= high, f"holding {start}+{count} is outside the count range"
    assert "holding" in schema["register_type"]["choices"]


def _handler(name: str) -> ast.AsyncFunctionDef:
    return next(
        node for node in ast.walk(DIAGNOSTIC_TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _own_returns(func: ast.AST) -> list:
    """Return statements of `func` itself, not of functions nested inside it."""
    found, stack = [], list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def test_get_register_data_returns_only_the_two_shapes_the_optimizer_parses():
    """RegisterVerifier._read_block digs a dict carrying "values" out of the response,
    treats success=False as UNVERIFIABLE (reading "error" for the log), and takes
    values[:count] as integers. Every return in the handler must be one of exactly two
    dict shapes; anything else - None, a bare list, a renamed key - reads as a failed
    verification on every slot."""
    returns = [n.value for n in _own_returns(_handler("get_register_data"))]
    assert returns, "get_register_data returns nothing"
    shapes = set()
    for node in returns:
        assert isinstance(node, ast.Dict), f"non-dict return: {ast.unparse(node)}"
        entry = dict(zip((ast.literal_eval(k) for k in node.keys), node.values))
        success = ast.literal_eval(entry["success"])
        if success:
            assert set(entry) == {"success", "values"}, ast.unparse(node)
        else:
            assert set(entry) == {"success", "values", "error"}, ast.unparse(node)
            assert ast.literal_eval(entry["values"]) == []
        shapes.add(success)
    assert shapes == {True, False}


# ---------------------------------------------------------------------------
# (c) the seven entity ids
# ---------------------------------------------------------------------------


def test_the_entity_replay_guards_the_optimizers_seven_ids():
    """test_wit_entity_keys.py replays setup against the live registry for its own copy
    of the seven; this holds that copy to the optimizer's configuration."""
    assert {k: v[0] for k, v in entity_keys.OPTIMIZER_SENSORS.items()} == OPTIMIZER_ENTITY_IDS


@pytest.mark.parametrize("key", sorted(set(OPTIMIZER_ENTITY_IDS) - {"wit_mode_status"}))
def test_display_name_and_device_still_produce_the_optimizers_entity_id(key):
    assert entity_keys._migrated_entity_id(key) == OPTIMIZER_ENTITY_IDS[key]


def test_the_inverter_mode_sensor_still_produces_its_entity_id():
    """Inverter device ("Growatt", no suffix) + name "Inverter Mode"."""
    _eid, name, device = entity_keys.OPTIMIZER_SENSORS["wit_mode_status"]
    assert (name, device) == ("Inverter Mode", "inverter")
    assert f"sensor.{entity_keys._slugify(entity_keys.DEVICE_NAME)}_" \
           f"{entity_keys._slugify(name)}" == OPTIMIZER_ENTITY_IDS["wit_mode_status"]
    assert entity_keys._sensor.SENSOR_DEFINITIONS["wit_mode_status"]["name"] == name


# ---------------------------------------------------------------------------
# (d) the Inverter Mode strings
# ---------------------------------------------------------------------------

_POWER_WORDS = (0, 1, 2, 50, 100, 65535, 65486, 65436)  # 65536-x = -x %
_MATRIX = list(itertools.product((0, 1), _POWER_WORDS, (0, 1), (0, 40, 100)))


@pytest.mark.parametrize("remote_enable,power_word,export_enable,export_rate", _MATRIX)
def test_mode_status_matches_the_optimizers_decoder(
    coordinator_module, growatt_modbus, remote_enable, power_word, export_enable,
    export_rate,
):
    """With both blocks read, _compute_wit_mode_status and the optimizer's port of it must
    name every register state the same way - the optimizer compares the strings."""
    data = growatt_modbus.GrowattData(
        vpp_remote_power_available=True,
        vpp_export_limit_available=True,
        remote_power_control_enable=remote_enable,
        remote_charge_and_discharge_power=power_word,
        vpp_export_limit_enable=export_enable,
        vpp_export_limit_power_rate=export_rate,
    )
    coordinator_module.GrowattModbusCoordinator._compute_wit_mode_status(None, data)
    expected = optimizer_decode_wit_mode_status({
        30407: remote_enable, 30409: power_word, 30200: export_enable, 30201: export_rate,
    })
    assert data.wit_mode_status == expected


def test_the_integration_spells_every_status_the_optimizer_expects(component_const):
    names = component_const.WIT_MODE_DISPLAY_NAMES
    for mode, status in OPTIMIZER_MODE_STATUS.items():
        assert names[mode] == status, f"{mode}: integration says {names[mode]!r}"
