"""Every option `services.yaml` offers has to be one the registered schema accepts.

`services.yaml` is the only thing a user sees. It drives the action dialog, and it is
what automations get copied from - but nothing validates it against the voluptuous schema
the handler is registered with, so the two can disagree indefinitely and the only symptom
is a `MultipleInvalid` when someone picks the wrong dropdown entry.

That happened to `set_battery_mode`. The selector listed `charge` / `discharge` /
`preserve_soc` while `SERVICE_SET_BATTERY_MODE_SCHEMA` was
`vol.In(["charge", "discharge", "hold"])` and the handler branched on `mode == "hold"`,
so the only non-charge/discharge option the UI offered was the one option that could not
be submitted. `hold` is back in the selector and `preserve_soc` is now an accepted alias,
so both spellings work.

Everything here is parsed from text. PyYAML is not a test dependency (the fast job
installs pytest, pymodbus and pyserial only), voluptuous is stubbed so a constructed
schema carries no usable structure, and the literals are the wire contract anyway.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
SERVICES_SRC = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
DIAGNOSTIC_SRC = (COMPONENT / "diagnostic.py").read_text(encoding="utf-8")
CONST_SRC = (COMPONENT / "const.py").read_text(encoding="utf-8")

# action -> (field, the schema constant that validates it)
SELECT_FIELDS = {
    "set_battery_mode": ("mode", "SERVICE_SET_BATTERY_MODE_SCHEMA"),
    "set_wit_mode": ("mode", "SERVICE_SET_WIT_MODE_SCHEMA"),
}


def _action_block(action: str) -> str:
    """The top-level services.yaml stanza for one action."""
    match = re.search(rf"^{action}:\n(.*?)(?=^\S|\Z)", SERVICES_SRC, re.S | re.M)
    assert match, f"{action} is not declared in services.yaml"
    return match.group(1)


def _field_block(action: str, field: str) -> str:
    block = _action_block(action)
    match = re.search(rf"^    {field}:\n(.*?)(?=^    \S|\Z)", block, re.S | re.M)
    assert match, f"{action} has no field {field!r}"
    return match.group(1)


def _yaml_options(action: str, field: str) -> list[str]:
    """Both selector spellings: labelled `- label:/value:` pairs and bare `- value`."""
    block = _field_block(action, field)
    options = re.search(r"^\s*options:\n(.*?)(?=^\s{0,8}\S|\Z)", block, re.S | re.M)
    assert options, f"{action}.{field} has no selector options"
    body = options.group(1)
    labelled = re.findall(r'^\s*value: "?([A-Za-z0-9_]+)"?\s*$', body, re.M)
    if labelled:
        return labelled
    return re.findall(r'^\s*- "?([A-Za-z0-9_]+)"?\s*$', body, re.M)


def _schema_block(constant: str) -> str:
    """Both brace styles are accepted, as elsewhere in this suite."""
    match = re.search(rf"^{constant} = vol\.Schema\((.*?)^\}}?\)", DIAGNOSTIC_SRC, re.S | re.M)
    assert match, f"{constant} is no longer a module-level vol.Schema"
    return match.group(1)


def _resolve_list(name: str) -> list[str]:
    """Follow `A = B` aliases in diagnostic.py through to the list literal in const.py."""
    for _ in range(4):
        alias = re.search(rf"^{name} = ([A-Z_][A-Z_0-9]*)$", DIAGNOSTIC_SRC, re.M)
        if not alias:
            break
        name = alias.group(1)
    for source in (DIAGNOSTIC_SRC, CONST_SRC):
        literal = re.search(rf"^{name} = (\[.*?\])$", source, re.S | re.M)
        if literal:
            return ast.literal_eval(literal.group(1))
    raise AssertionError(f"cannot resolve {name} to a list literal")


def _schema_values(constant: str, field: str) -> list[str]:
    """The values `vol.In(...)` accepts for one required field of one schema."""
    block = _schema_block(constant)
    match = re.search(
        rf'vol\.Required\("{field}"\): vol\.In\(\s*(\[.*?\]|[A-Z_][A-Z_0-9]*)\s*\)',
        block, re.S,
    )
    assert match, f"{constant}: no vol.In for {field!r}"
    literal = match.group(1)
    if literal.startswith("["):
        return ast.literal_eval(literal)
    return _resolve_list(literal)


def _handler_source(name: str) -> str:
    tree = ast.parse(DIAGNOSTIC_SRC)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    return ast.get_source_segment(DIAGNOSTIC_SRC, func)


@pytest.mark.parametrize("action", sorted(SELECT_FIELDS))
def test_every_offered_option_is_accepted_by_the_schema(action: str) -> None:
    field, constant = SELECT_FIELDS[action]
    offered = _yaml_options(action, field)
    accepted = _schema_values(constant, field)
    assert offered, f"{action}.{field} has no selector options to check"
    unusable = [option for option in offered if option not in accepted]
    assert not unusable, (
        f"{action}.{field} offers {unusable} in the UI but the registered schema only "
        f"accepts {accepted}; selecting one raises vol.Invalid and the action never runs"
    )


def test_set_battery_mode_still_offers_a_standby_option() -> None:
    """The bug removed the working option and left only the broken one, so "the selector
    and the schema agree" is not sufficient on its own - agreeing on two modes would pass
    while standby had quietly disappeared."""
    assert "hold" in _yaml_options("set_battery_mode", "mode")


def test_the_fork_spelling_of_standby_is_still_accepted() -> None:
    """Anything written against the fork's wording keeps working; the alias is why the
    selector could go back to upstream's `hold` without breaking automations."""
    accepted = _schema_values("SERVICE_SET_BATTERY_MODE_SCHEMA", "mode")
    assert {"hold", "preserve_soc"} <= set(accepted)

    handler = _handler_source("set_battery_mode")
    assert 'mode == "preserve_soc"' in handler and 'mode = "hold"' in handler, (
        "preserve_soc is accepted by the schema but not normalised, so it falls through "
        "every branch and the action silently does nothing"
    )


def test_the_tou_standby_power_word_is_documented_as_one_percent() -> None:
    """+1% is standby; 0 is not, and -1% is a full discharge.

    The asymmetry is real hardware behaviour and is spelled out at both places that write
    the TOU period power word - `set_battery_mode`'s hold branch and the VPP Battery Mode
    select, both of which write 1. The action's own docstring says power=1 too. Only
    services.yaml said 0, which is what a user copies.
    """
    block = _action_block("sync_tou_schedule")
    assert "power=1" in block
    assert "power=0" not in block

    example = re.search(r"example: '(\[.*?\])'", block, re.S)
    assert example, "the worked example is gone"
    powers = {period["power"] for period in ast.literal_eval(example.group(1))}
    assert 0 not in powers, (
        "the worked example still shows a standby period as power=0, which does not idle "
        "the battery on this hardware"
    )

    for source in (_handler_source("set_battery_mode"),
                   (COMPONENT / "select.py").read_text(encoding="utf-8")):
        assert "[start_min, end_min, 1]" in source, (
            "the code that actually writes a TOU standby period no longer uses +1%; "
            "services.yaml and this test need revisiting together"
        )
