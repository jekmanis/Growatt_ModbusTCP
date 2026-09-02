"""The WIT mode tables in const.py have to be the ones the code actually uses.

`WIT_MODES`, `WIT_MODE_DISPLAY_NAMES` and `WIT_AC_CHARGE_MODES` were declared in const.py
and referenced nowhere. diagnostic.py kept its own `WIT_MODE_CHOICES` / `AC_CHARGE_MODE_MAP`
with the same contents, and the coordinator spelled the display names out as literals.
Three copies of one table, one of them authoritative-looking and inert: editing the const
version changed nothing, and the copies could drift in either direction without any test
noticing - on a service contract that battery_optimizer sends and reads verbatim.

`GrowattData.wit_mode_duration_remaining` was the same shape of dead weight, with one
extra edge: nothing ever wrote it, so `diagnostics.py`'s `asdict(data)` published a
constant 0 as if it were a reading.
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
_const = importlib.import_module("growatt_under_test.const")
_diag = importlib.import_module("growatt_under_test.diagnostic")
_gm = importlib.import_module("growatt_under_test.growatt_modbus")

COORDINATOR_SRC = (COMPONENT / "coordinator.py").read_text(encoding="utf-8")
SELECT_SRC = (COMPONENT / "select.py").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "name", ["WIT_MODES", "WIT_MODE_DISPLAY_NAMES", "WIT_AC_CHARGE_MODES"]
)
def test_the_table_has_a_consumer_outside_const(name: str) -> None:
    """A table nothing reads is not documentation, it is a decoy."""
    users = [
        path.name for path in sorted(COMPONENT.glob("*.py"))
        if path.name != "const.py" and re.search(rf"\b{name}\b", path.read_text(encoding="utf-8"))
    ]
    assert users, f"{name} is declared in const.py and used nowhere"


def test_the_service_schema_uses_the_const_mode_list() -> None:
    """These strings are the wire contract: battery_optimizer sends `grid_charge`,
    `preserve_soc`, `passthrough` and the rest by name."""
    assert _diag.WIT_MODE_CHOICES is _const.WIT_MODES
    assert _diag.AC_CHARGE_MODE_MAP is _const.WIT_AC_CHARGE_MODES


def test_the_mode_sensor_reports_names_from_the_table() -> None:
    """`sensor.growatt_inverter_mode` feeds battery_optimizer's SlotOutcomeTracker, and
    `select.WIT_MODE_PRESETS` is keyed by the same strings; a literal in the coordinator
    is a fourth place for them to disagree."""
    assert "WIT_MODE_DISPLAY_NAMES" in COORDINATOR_SRC
    body = COORDINATOR_SRC[COORDINATOR_SRC.index("def _compute_wit_mode_status"):]
    body = body[:body.index("\n    @property")]
    literals = re.findall(r'wit_mode_status = "([^"]+)"', body)
    # "Unknown" is not a mode - it is the absence of an answer - and stays a literal.
    assert set(literals) == {"Unknown"}, literals


def test_every_display_name_the_coordinator_can_emit_is_in_the_table() -> None:
    body = COORDINATOR_SRC[COORDINATOR_SRC.index("def _compute_wit_mode_status"):]
    keys = set(re.findall(r'WIT_MODE_DISPLAY_NAMES\["([a-z_]+)"\]', body))
    assert keys <= set(_const.WIT_MODE_DISPLAY_NAMES), keys
    assert keys == {"passthrough", "grid_charge", "preserve_soc", "max_export",
                    "discharge_to_grid", "discharge_to_load"}


def test_the_dashboard_presets_are_keyed_by_the_same_names() -> None:
    """WIT_MODE_PRESETS keys are what the Mode Preset select shows AND what
    `current_option` compares the polled mode against, so a preset whose key is not a
    value of the table can never read back as selected."""
    presets = set(re.findall(r'^    "([A-Za-z ]+)": \{$', SELECT_SRC, re.M))
    assert presets, "WIT_MODE_PRESETS keys not found"
    assert presets <= set(_const.WIT_MODE_DISPLAY_NAMES.values()), (
        sorted(presets - set(_const.WIT_MODE_DISPLAY_NAMES.values()))
    )


def test_hold_and_preserve_soc_display_the_same_way() -> None:
    """They are one mode with two spellings - `hold` is the legacy name - and the register
    sequence is identical. Anything else would make the same inverter state read as two
    different modes."""
    names = _const.WIT_MODE_DISPLAY_NAMES
    assert names["hold"] == names["preserve_soc"] == "Preserve SOC"


def test_the_table_covers_every_mode_the_service_accepts() -> None:
    assert set(_const.WIT_MODES) == set(_const.WIT_MODE_DISPLAY_NAMES)


def test_the_never_written_duration_field_is_gone() -> None:
    """`diagnostics.py` serialises GrowattData with `asdict`, so an unwritten field is
    not invisible - it is published as a reading of 0."""
    assert not hasattr(_gm.GrowattData(), "wit_mode_duration_remaining")

    written = set(re.findall(r"data\.(wit_mode_[a-z_]+)\s*=", COORDINATOR_SRC))
    declared = {
        field for field in _gm.GrowattData().__dataclass_fields__
        if field.startswith("wit_mode_")
    }
    assert declared == written, (
        f"declared but never written: {sorted(declared - written)}; "
        f"written but not declared: {sorted(written - declared)}"
    )


def test_the_sensor_attribute_still_reports_a_remaining_duration() -> None:
    """Removing the field must not remove the attribute. It is derived from what this
    process knows - the timestamp and duration of the last set_wit_mode - because 30408
    does not count down."""
    sensor_src = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    cls = next(
        ast.get_source_segment(sensor_src, node)
        for node in ast.walk(ast.parse(sensor_src))
        if isinstance(node, ast.ClassDef) and node.name == "GrowattWitModeStatusSensor"
    )
    assert '"duration_remaining_minutes"' in cls
    assert "wit_direct_mode_timestamp" in cls
