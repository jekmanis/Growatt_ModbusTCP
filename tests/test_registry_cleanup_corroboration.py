"""Deleting a registry row needs more evidence than one poll.

`__init__.async_setup_entry` removes control entities whose VPP block "is not
responsive". Those flags - `vpp_export_limit_available`, `vpp_control_authority_available`
- are PER-POLL: growatt_modbus sets them only inside the successful branch of that poll's
30200 / 30100 read, and GrowattData is rebuilt every poll. One unanswered read leaves them
False, and that is below the optional-holding backoff's own threshold, so it is not even a
skipped block - just a dropped frame.

The block used to sample the first poll carrying a serial number, act on it and latch. It
had been dead code for as long as it was gated on setup-time data (which
`async_config_entry_first_refresh` never populates), which is why the reference
installation still has all three rows; v1.8.14 moved it onto the coordinator listener and
made it live. One dropped frame then removed `select.growatt_grid_control_authority`,
`select.growatt_grid_vpp_export_limit_enable` and
`number.growatt_grid_vpp_export_limit_power_rate` from the registry - names, areas and
dashboard references included - permanently, because the latch was set before the decision
was made.

The second half of this module covers the opposite failure: a row that nothing can create
and nothing can remove. `number.growatt_vpp_active_power_rate` sits in the registry from
before number.py's WIT branch existed. On WIT that branch creates
`{entry}_active_power_rate_vpp` and returns before the generic WRITABLE_REGISTERS loop
that would create `{entry}_active_power_rate`, and the blanket control cleanup keeps the
old row because register 201 IS in the WIT holding map and IS writable.

__init__.py imports the Home Assistant entity stack, which the HA-free suite cannot load,
so the listener is lifted out of the module and executed on its own against fakes. That is
worth the machinery: this is a branch no other test can reach, guarding a destructive
action.
"""
from __future__ import annotations

import ast
import importlib
import re
import textwrap
from pathlib import Path

import pytest

COMPONENT = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
INIT_SRC = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
_const = importlib.import_module("growatt_under_test.const")
_gm = importlib.import_module("growatt_under_test.growatt_modbus")

ENTRY_ID = "01KBB0DFK8WSEB83341HYNM1MX"


def _source_of(name: str, kind=(ast.FunctionDef, ast.AsyncFunctionDef)) -> str:
    for node in ast.walk(ast.parse(INIT_SRC)):
        if isinstance(node, kind) and node.name == name:
            return ast.get_source_segment(INIT_SRC, node) or ""
    raise AssertionError(f"{name} not found in __init__.py")


SETUP = _source_of("async_setup_entry")
CLEANUP = _source_of("_cleanup_unsupported_vpp_entities")
THRESHOLD = int(re.search(r"^VPP_CLEANUP_CONSECUTIVE_POLLS = (\d+)$", INIT_SRC, re.M).group(1))


# ---------------------------------------------------------------------------
# A runnable copy of the listener
# ---------------------------------------------------------------------------


class _Registry:
    """Entity registry with the three rows the reference installation actually has."""

    def __init__(self) -> None:
        self.rows = {
            ("select", f"{ENTRY_ID}_control_authority"): "select.growatt_grid_control_authority",
            ("select", f"{ENTRY_ID}_vpp_export_limit_enable"):
                "select.growatt_grid_vpp_export_limit_enable",
            ("number", f"{ENTRY_ID}_vpp_export_limit_power_rate"):
                "number.growatt_grid_vpp_export_limit_power_rate",
        }
        self.removed: list[str] = []

    def async_get_entity_id(self, platform, domain, unique_id):
        return self.rows.get((platform, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.rows = {k: v for k, v in self.rows.items() if v != entity_id}


class _Coordinator:
    def __init__(self) -> None:
        self.data = None


def _listener():
    """Rebuild the closure as a module-level function over fakes.

    The `nonlocal`-free shape of the fixed code is what makes this possible; the counters
    are mutated in place rather than rebound, so they can be handed in as globals.
    """
    body = "\n".join(
        line for line in textwrap.dedent(CLEANUP).splitlines()
        if not line.startswith("@")
    )
    registry = _Registry()
    coordinator = _Coordinator()
    namespace = {
        "coordinator": coordinator,
        "entry": type("Entry", (), {"entry_id": ENTRY_ID})(),
        "hass": object(),
        "er": type("er", (), {"async_get": staticmethod(lambda hass: registry)}),
        "DOMAIN": "growatt_modbus",
        "_LOGGER": type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                  "debug": staticmethod(lambda *a, **k: None)}),
        "VPP_CLEANUP_CONSECUTIVE_POLLS": THRESHOLD,
        "_vpp_absent_polls": {"export": 0, "authority": 0},
        "_vpp_settled": set(),
    }
    exec(compile(body, "<cleanup>", "exec"), namespace)
    return namespace["_cleanup_unsupported_vpp_entities"], coordinator, registry


def _poll(export_ok: bool, authority_ok: bool, serial: str = "AB12345678"):
    return _gm.GrowattData(
        serial_number=serial,
        vpp_export_limit_available=export_ok,
        vpp_control_authority_available=authority_ok,
    )


# ---------------------------------------------------------------------------
# 1. The VPP removal needs corroboration
# ---------------------------------------------------------------------------


def test_one_missed_poll_removes_nothing() -> None:
    """The reported failure, run end to end: a single dropped 30200 read used to delete
    both export-limit entities and latch the decision."""
    listener, coordinator, registry = _listener()
    coordinator.data = _poll(export_ok=False, authority_ok=True)
    listener()
    assert registry.removed == []


def test_removal_happens_only_after_the_block_has_really_stopped_answering() -> None:
    listener, coordinator, registry = _listener()
    coordinator.data = _poll(export_ok=False, authority_ok=True)
    for _ in range(THRESHOLD - 1):
        listener()
        assert registry.removed == []
    listener()
    assert sorted(registry.removed) == [
        "number.growatt_grid_vpp_export_limit_power_rate",
        "select.growatt_grid_vpp_export_limit_enable",
    ]
    # control_authority answered, so it is never touched.
    assert "select.growatt_grid_control_authority" not in registry.removed


def test_an_intermittent_block_is_never_removed() -> None:
    """Alternating miss/answer is the shape of a marginal bus, not of a missing register.
    Anything short of an unbroken run has to reset the count."""
    listener, coordinator, registry = _listener()
    for _ in range(20):
        coordinator.data = _poll(export_ok=False, authority_ok=False)
        listener()
        coordinator.data = _poll(export_ok=True, authority_ok=True)
        listener()
    assert registry.removed == []


def test_a_single_answer_settles_the_question_for_good() -> None:
    """The asymmetry is the point. A register that replied once exists; nothing later can
    make that untrue, so the decision can be latched immediately - and must be, or a long
    outage would eventually delete a control that works."""
    listener, coordinator, registry = _listener()
    coordinator.data = _poll(export_ok=True, authority_ok=True)
    listener()
    for _ in range(THRESHOLD * 3):
        coordinator.data = _poll(export_ok=False, authority_ok=False)
        listener()
    assert registry.removed == []


def test_a_block_that_never_answers_is_still_cleaned_up() -> None:
    """The feature has to keep working: an inverter without the VPP registers should not
    keep three permanently-unavailable controls."""
    listener, coordinator, registry = _listener()
    for _ in range(THRESHOLD):
        coordinator.data = _poll(export_ok=False, authority_ok=False)
        listener()
    assert len(registry.removed) == 3


def test_nothing_happens_before_a_real_poll() -> None:
    """Unchanged and load-bearing: an empty placeholder cannot distinguish an unsupported
    register from an inverter that is simply offline (#255). It must not even count as a
    miss."""
    listener, coordinator, registry = _listener()
    for _ in range(THRESHOLD * 2):
        coordinator.data = None
        listener()
        coordinator.data = _poll(export_ok=False, authority_ok=False, serial="")
        listener()
    assert registry.removed == []


def test_the_threshold_matches_the_clients_own_give_up_point() -> None:
    """At `_OPTIONAL_HOLDING_FAIL_THRESHOLD` consecutive failures growatt_modbus stops
    asking for the block. Removing entities earlier means removing them while the client
    still considers the register worth retrying."""
    client_src = (COMPONENT / "growatt_modbus.py").read_text(encoding="utf-8")
    client_value = int(
        re.search(r"^_OPTIONAL_HOLDING_FAIL_THRESHOLD = (\d+)$", client_src, re.M).group(1)
    )
    assert THRESHOLD == client_value >= 2


def test_the_old_single_poll_latch_is_gone() -> None:
    """`_vpp_cleanup_done = True` was set before either flag was examined, so a poll that
    decided nothing still closed the question forever."""
    assert "_vpp_cleanup_done" not in SETUP


# ---------------------------------------------------------------------------
# 2. The orphan the blanket rule cannot reach
# ---------------------------------------------------------------------------


def test_the_wit_superseded_generic_control_is_removed() -> None:
    assert "_active_power_rate" in SETUP, (
        "nothing removes number.growatt_vpp_active_power_rate; on WIT it can be neither "
        "created nor cleaned up and stays unavailable forever"
    )


def test_the_removal_is_scoped_to_wit_profiles() -> None:
    """On every non-WIT profile number.py's generic loop DOES create
    `{entry}_active_power_rate`, so removing it unconditionally would delete a live
    entity on every setup."""
    marker = SETUP.index('f"{entry.entry_id}_active_power_rate"')
    guard = SETUP.rindex("if ", 0, marker)
    assert "WIT_REGISTER_MAPS" in SETUP[guard:marker], SETUP[guard:marker]


def test_the_blanket_control_rule_really_cannot_reach_it() -> None:
    """Why a named block is needed at all: the general rule keeps any control whose
    register is present and writable, and 201 is both on WIT."""
    holding = _const.REGISTER_MAPS["WIT_4000_15000TL3"]["holding_registers"]
    register = _const.WRITABLE_REGISTERS["active_power_rate"]["register"]
    assert register in holding
    assert not _const.is_read_only_register(holding[register])


def test_the_wit_entity_that_supersedes_it_uses_a_different_suffix() -> None:
    """If the WIT class ever adopted `active_power_rate` the removal above would delete a
    live entity instead of an orphan."""
    number_src = (COMPONENT / "number.py").read_text(encoding="utf-8")
    cls = next(
        node for node in ast.walk(ast.parse(number_src))
        if isinstance(node, ast.ClassDef) and node.name == "GrowattWitActivePowerRateNumber"
    )
    # GrowattEntity.__init__(coordinator, config_entry, unique_key, device_type)
    unique_keys = [
        call.args[2].value
        for call in ast.walk(cls)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute) and call.func.attr == "__init__"
        and len(call.args) >= 3 and isinstance(call.args[2], ast.Constant)
    ]
    assert unique_keys == ["active_power_rate_vpp"], (
        f"the WIT entity's unique_id suffix is {unique_keys}; if it ever claims the "
        f"generic 'active_power_rate' the removal above deletes a live entity"
    )


@pytest.mark.parametrize("key", [
    "battery_soc", "pv_total_power", "battery_charge_today",
    "battery_discharge_today", "house_consumption", "wit_mode_status",
])
def test_no_removal_block_names_an_optimizer_entity(key: str) -> None:
    """Guard rail. battery_optimizer resolves seven entities by ID and cannot rediscover
    them; a removal keyed on one of those suffixes would delete an input it depends on.
    (battery_temp is deliberately excluded - it has a legitimate, profile-scoped removal
    for MOD/MID, where register 3176 is the DC-DC stage, not the pack.)"""
    assert f'f"{{entry.entry_id}}_{key}"' not in SETUP
