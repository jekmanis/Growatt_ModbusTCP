"""Every SENSOR_DEFINITIONS key needs an `entity.sensor.<key>.name` in both string files.

Upstream v1.8.14 moved sensor display names out of `sensor_def["name"]` into
`strings.json` / `translations/en.json`: the generic sensor sets
`self._attr_translation_key = sensor_key` and Home Assistant looks the name up there.
`tests_ha/test_entity_translations.py` enforces the mapping - but it only runs in the
Linux CI job (pytest-homeassistant-custom-component cannot build on Windows), so a
missing entry produces no local signal at all.

The re-ported `wit_mode_status` had none. It survived because
`GrowattWitModeStatusSensor` sets `_attr_name` directly, so the entity kept working and
kept its ID; the only symptoms were three red assertions in a job nobody runs locally,
plus a name hardcoded in English across all 22 shipped languages.

This is the same check, one direction only (definition -> translation) and without
importing Home Assistant. The reverse direction - orphaned translations - stays in
tests_ha, because the generated keys (`_pv_string_sensors` and friends) are invisible to
a source scan and would look like orphans here.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
SENSOR_SRC = (COMPONENT / "sensor.py").read_text(encoding="utf-8")

STRING_FILES = {
    "strings.json": COMPONENT / "strings.json",
    "translations/en.json": COMPONENT / "translations" / "en.json",
}


def _explicit_sensor_keys() -> dict[str, str]:
    """key -> name, for keys written literally in the SENSOR_DEFINITIONS dict.

    `**_pv_string_sensors`-style spreads are not resolvable from source and are skipped;
    they only cost false negatives, and the reverse check in tests_ha covers them.
    """
    tree = ast.parse(SENSOR_SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "SENSOR_DEFINITIONS" for t in node.targets
        ):
            continue
        found: dict[str, str] = {}
        for key, value in zip(node.value.keys, node.value.values):
            if key is None or not isinstance(key, ast.Constant):
                continue  # a ** spread
            name = next(
                (
                    v.value for k, v in zip(value.keys, value.values)
                    if isinstance(k, ast.Constant) and k.value == "name"
                    and isinstance(v, ast.Constant)
                ),
                None,
            )
            if name is not None:
                found[key.value] = name
        return found
    raise AssertionError("SENSOR_DEFINITIONS is no longer a module-level dict literal")


SENSOR_NAMES = _explicit_sensor_keys()


def _entity_sensor_names(path: Path) -> dict[str, str]:
    entity = json.loads(path.read_text(encoding="utf-8")).get("entity", {})
    return {
        key: value.get("name")
        for key, value in entity.get("sensor", {}).items()
    }


def test_the_scan_found_the_definitions() -> None:
    """A regex/ast scan that quietly matches nothing passes every assertion below."""
    assert len(SENSOR_NAMES) > 100
    assert SENSOR_NAMES.get("battery_soc") == "Battery SOC"


@pytest.mark.parametrize("filename", sorted(STRING_FILES))
def test_every_sensor_definition_has_a_translated_name(filename: str) -> None:
    names = _entity_sensor_names(STRING_FILES[filename])
    missing = sorted(key for key in SENSOR_NAMES if key not in names)
    assert not missing, (
        f"{filename} has no entity.sensor entry for {missing}; "
        f"tests_ha/test_entity_translations.py fails in CI and the entity name cannot "
        f"be translated"
    )


@pytest.mark.parametrize("filename", sorted(STRING_FILES))
def test_the_english_text_matches_the_definition(filename: str) -> None:
    names = _entity_sensor_names(STRING_FILES[filename])
    mismatched = {
        key: (name, names[key])
        for key, name in SENSOR_NAMES.items()
        if key in names and names[key] != name
    }
    assert not mismatched, f"{filename}: definition vs translation {mismatched}"


def test_wit_mode_status_is_named_in_both_files() -> None:
    """Named explicitly: it is the entity battery_optimizer resolves by ID, and
    `__init__._migrate_entity_ids` derives `sensor.growatt_inverter_mode` from exactly
    this string on every setup."""
    for filename, path in STRING_FILES.items():
        assert _entity_sensor_names(path).get("wit_mode_status") == "Inverter Mode", filename
