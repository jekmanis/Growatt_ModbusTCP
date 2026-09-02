"""The set_wit_mode / get_register_data contract the battery optimizer depends on.

`battery_optimizer` (an AppDaemon app, source at
appdaemon/apps/battery_optimizer_lib/direct_control.py) is the only consumer that
drives this integration programmatically, and it drives it through exactly two
actions:

  * `growatt_modbus.set_wit_mode` — its ENTIRE write path. It classifies a raised
    call or a `success=False` response as a CONFIRMED failure and stops trusting
    the schedule slot; anything else counts as sent.
  * `growatt_modbus.get_register_data` — its verification read. It reads holding
    30407 count 4 and 30200 count 2 and compares them against the registers it
    predicts `set_wit_mode` wrote, using its own `expected_registers()` derived
    from the params it sent.

That second half is the load-bearing part, and it is why this module exists.
The optimizer does not verify against `sensor.growatt_inverter_mode`: on
2026-09-01 one transient read froze that sensor at "Passthrough" for 30 h, and
the 2026-09-02 log then carried 73/73 false mismatches, each paying for a
blocking resend. Register verification was the fix, and it only works while the
handler's register sequence and the optimizer's prediction of it agree
register-for-register. Nothing in either tree checks that they still do — the
optimizer's tests stub the service, and this repo's tests never call the handler.
A silent divergence here does not fail loudly: it produces a MISMATCH, a resend,
and then an ERROR, on every slot, while the inverter is doing the right thing.

So `expected_registers()` below is a verbatim transcription of the optimizer's
function. It is deliberately duplicated rather than summarised: the test's value
is that it fails when this repo changes and the optimizer does not.

The client is a real `GrowattModbus` over an in-memory register bank rather than
a stub, so the write path exercised here is the production one — `write_batch`,
the per-register rate limiter and its bypass, and the FC 0x06 -> FC 0x10 fallback
on 30410 — and the read-back goes through the same `read_holding_registers` the
action calls. Only pymodbus itself is faked.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

_diag = importlib.import_module("growatt_under_test.diagnostic")
_gm = importlib.import_module("growatt_under_test.growatt_modbus")
_const = importlib.import_module("growatt_under_test.const")

WIT_MAP = "WIT_4000_15000TL3"
DIAGNOSTIC_SRC = (
    Path(__file__).parent.parent
    / "custom_components" / "growatt_modbus" / "diagnostic.py"
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The optimizer's side of the contract, transcribed from
# battery_optimizer_lib/direct_control.py. Keep byte-compatible with it.
# ---------------------------------------------------------------------------

REG_EXPORT_LIMIT_ENABLE = 30200
REG_EXPORT_LIMIT_RATE = 30201
REG_REMOTE_ENABLE = 30407
REG_REMOTE_DURATION = 30408
REG_REMOTE_POWER = 30409
REG_AC_CHARGE_ENABLE = 30410

REG_BLOCK_CONTROL = (REG_REMOTE_ENABLE, 4)
REG_BLOCK_EXPORT = (REG_EXPORT_LIMIT_ENABLE, 2)

AC_CHARGE_MODE_VALUES = {"disabled": 0, "pv_priority": 1, "ac_priority": 2}

MODE_STATUS_MAP = {
    "grid_charge": "Grid Charge",
    "hold": "Preserve SOC",
    "preserve_soc": "Preserve SOC",
    "max_export": "Max Export",
    "discharge_to_grid": "Discharge to Grid",
    "discharge_to_load": "Discharge to Load",
    "passthrough": "Passthrough",
}


def decode_signed_power(raw: int) -> int:
    """Two's-complement decode of 30409: 65436 is -100 %, not a 65436 % charge."""
    raw = int(raw)
    return raw - 65536 if raw > 32767 else raw


def expected_registers(mode_str: str, params: dict):
    """VERBATIM port of direct_control.expected_registers().

    Derived from the params the optimizer SENT, not from a static per-mode table,
    because the same mode writes different registers depending on export_rate.
    Each value is a tuple of acceptable values: 30410=2 has a documented firmware
    fallback to 1.
    """
    if mode_str not in MODE_STATUS_MAP:
        return None

    power = int(params.get("power_percent", 100) or 100)
    export_rate = params.get("export_rate")
    ac_mode = params.get("ac_charge_mode")

    exp = {}

    # --- Step 2: AC charge mode (30410) ---
    if ac_mode is not None:
        ac_val = AC_CHARGE_MODE_VALUES.get(ac_mode)
        if ac_val == 2:
            exp[REG_AC_CHARGE_ENABLE] = (2, 1)
        elif ac_val is not None:
            exp[REG_AC_CHARGE_ENABLE] = (ac_val,)
    elif mode_str == "grid_charge":
        exp[REG_AC_CHARGE_ENABLE] = (1,)
    else:
        exp[REG_AC_CHARGE_ENABLE] = (0,)

    # --- Step 4: export limit (30200 / 30201) ---
    if export_rate is not None:
        if int(export_rate) >= 100:
            exp[REG_EXPORT_LIMIT_ENABLE] = (0,)
        else:
            exp[REG_EXPORT_LIMIT_ENABLE] = (1,)
            exp[REG_EXPORT_LIMIT_RATE] = (int(export_rate),)
    elif mode_str == "discharge_to_load":
        exp[REG_EXPORT_LIMIT_ENABLE] = (1,)
        exp[REG_EXPORT_LIMIT_RATE] = (0,)
    else:
        exp[REG_EXPORT_LIMIT_ENABLE] = (0,)

    # --- Step 5: battery power command (30407 / 30409) ---
    if mode_str == "passthrough":
        exp[REG_REMOTE_ENABLE] = (0,)
        exp[REG_REMOTE_POWER] = (0,)
    elif mode_str in ("hold", "preserve_soc"):
        exp[REG_REMOTE_ENABLE] = (1,)
        exp[REG_REMOTE_POWER] = (1,)
    elif mode_str == "grid_charge":
        exp[REG_REMOTE_ENABLE] = (1,)
        exp[REG_REMOTE_POWER] = (power,)
    else:  # discharge_to_load / discharge_to_grid / max_export
        p = 100 if mode_str == "max_export" else power
        exp[REG_REMOTE_ENABLE] = (1,)
        exp[REG_REMOTE_POWER] = (65536 - p,)

    return exp


def differences_like_the_verifier(observed: dict, expected: dict) -> list:
    """RegisterVerifier.__call__'s comparison, including the 30201 skip rule.

    30201 is compared only when 30200 is expected to be 1: with the limiter off
    the action never writes the rate, so whatever stale value sits there is not
    evidence of anything.
    """
    out = []
    for reg, accepted in expected.items():
        if (reg == REG_EXPORT_LIMIT_RATE
                and 1 not in expected.get(REG_EXPORT_LIMIT_ENABLE, (1,))):
            continue
        actual = observed.get(reg)
        if actual not in accepted:
            out.append(f"{reg}={actual} (expected {'/'.join(map(str, accepted))})")
    return out


# ---------------------------------------------------------------------------
# Fakes: pymodbus only. Everything above it is production code.
# ---------------------------------------------------------------------------


class _Ok:
    def isError(self):
        return False


class _Err:
    def isError(self):
        return True

    def __repr__(self):
        return "ExceptionResponse(IllegalFunction)"


class _Read:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


class _FakeModbus:
    """In-memory holding-register bank standing in for a pymodbus client.

    `refuse` holds (register, value) pairs the device rejects, and `refuse_fc06`
    holds registers that reject Write Single Register but accept Write Multiple —
    the real reported behaviour of 30410 on WIT firmware (#353).
    """

    def __init__(self, seed=None, refuse=(), refuse_fc06=()):
        self.registers = dict(seed or {})
        self.refuse = set(refuse)
        self.refuse_fc06 = set(refuse_fc06)
        self.calls = []  # (function_code, address, values)

    # -- connection surface GrowattModbus._ensure_connection and
    # SharedModbusConnection.ensure_connected touch
    socket = object()

    def connect(self):
        return True

    def is_socket_open(self):
        return True

    def close(self):
        pass

    # -- writes
    def write_register(self, address=None, value=None, **kwargs):
        self.calls.append(("fc06", address, [value]))
        if address in self.refuse_fc06 or (address, value) in self.refuse:
            return _Err()
        self.registers[address] = value
        return _Ok()

    def write_registers(self, address=None, values=None, **kwargs):
        values = list(values)
        self.calls.append(("fc10", address, values))
        if any((address + i, v) in self.refuse for i, v in enumerate(values)):
            return _Err()
        for i, v in enumerate(values):
            self.registers[address + i] = v
        return _Ok()

    # -- reads
    def read_holding_registers(self, address=None, count=None, **kwargs):
        self.calls.append(("fc03", address, [count]))
        return _Read([self.registers.get(address + i, 0) for i in range(count)])


# Leftover state from an earlier slot. Every value here is one NO command in this
# module produces, so a passing assertion can only come from a write that actually
# happened — a bank seeded with the right answers would let the tests pass while
# the sequence wrote nothing. 30201=37 additionally covers the verifier's skip
# rule: modes that leave the limiter off never write the rate, and the stale value
# must not be read as evidence.
STALE_BANK = {30200: 1, 30201: 37, 30407: 1, 30408: 99, 30409: 12345, 30410: 2}


def _client(seed=None, refuse=(), refuse_fc06=()):
    client = _gm.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502, register_map=WIT_MAP
    )
    fake = _FakeModbus(
        seed=STALE_BANK if seed is None else seed,
        refuse=refuse,
        refuse_fc06=refuse_fc06,
    )
    client.client = fake
    # The 250 ms inter-read spacing is a real bus constraint, not a contract one.
    client.min_read_interval = 0
    client._default_min_read_interval = 0
    client._fake = fake
    return client


class _Coordinator:
    def __init__(self, client):
        self._client = client
        self.refreshed = 0
        # Set by set_wit_mode; declared here because the real coordinator does.
        self.wit_direct_mode = None
        self.wit_direct_mode_power = 0
        self.wit_direct_mode_duration = 0
        self.wit_direct_mode_timestamp = None
        self.wit_direct_mode_source = ""
        self.wit_direct_export_rate = None
        self.wit_direct_ac_charge_mode = None

    @property
    def modbus_client(self):
        return self._client

    async def async_request_refresh(self):
        self.refreshed += 1


DEVICE_ID = "05005d2cc8b5b7acce146af1698e9fb3"
ENTRY_ID = "01KBB0DFK8WSEB83341HYNM1MX"


class _DeviceEntry:
    config_entry_id = ENTRY_ID


class _Hass:
    """Only the surface the two handlers touch."""

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.registered = {}
        self.services = self
        self.config_entries = self

    # hass.services
    def async_register(self, domain, service, handler, schema=None,
                       supports_response=None):
        self.registered[service] = {
            "domain": domain,
            "handler": handler,
            "schema": schema,
            "supports_response": supports_response,
        }

    # hass.config_entries
    def async_get_entry(self, entry_id):
        if entry_id != ENTRY_ID:
            return None
        return type("Entry", (), {"runtime_data": self.coordinator})()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _Call:
    def __init__(self, **data):
        self.data = data


class _FakeDeviceRegistry:
    @staticmethod
    def async_get(device_id):
        return _DeviceEntry() if device_id == DEVICE_ID else None


class _FakeDrModule:
    @staticmethod
    def async_get(hass):
        return _FakeDeviceRegistry()


@pytest.fixture
def wired(monkeypatch):
    """(hass, coordinator, client, handlers) with the real handlers registered."""

    def _build(client=None):
        client = client if client is not None else _client()
        coordinator = _Coordinator(client)
        hass = _Hass(coordinator)
        monkeypatch.setattr(_diag, "dr", _FakeDrModule)
        asyncio.run(_diag.async_setup_services(hass))
        return hass, coordinator, client, hass.registered

    return _build


def _set_wit_mode(handlers, **params):
    return asyncio.run(handlers["set_wit_mode"]["handler"](_Call(**params)))


def _get_register_data(handlers, start_address, count):
    return asyncio.run(handlers["get_register_data"]["handler"](_Call(
        device_id=DEVICE_ID, register_type="holding",
        start_address=start_address, count=count,
    )))


def _observe(handlers):
    """Read back exactly what RegisterVerifier reads: two blocks, two calls."""
    control = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    export = _get_register_data(handlers, *REG_BLOCK_EXPORT)
    assert control["success"] is True and export["success"] is True
    return {
        REG_REMOTE_ENABLE: control["values"][0],
        REG_REMOTE_DURATION: control["values"][1],
        REG_REMOTE_POWER: control["values"][2],
        REG_AC_CHARGE_ENABLE: control["values"][3],
        REG_EXPORT_LIMIT_ENABLE: export["values"][0],
        REG_EXPORT_LIMIT_RATE: export["values"][1],
    }


# ---------------------------------------------------------------------------
# (a) The registers written match what direct_control.py predicts
# ---------------------------------------------------------------------------

# Exactly what DirectControl.apply_mode_with_outcome / release_control build:
# device_id + mode + duration_minutes + power_percent, plus export_rate when the
# schedule entry carries one, ac_charge_mode from _ac_charge_mode_for_entry
# ("disabled" for every non-CHARGE slot, so it is always present), and the SOC
# cutoff for the direction being commanded. release_control sends only device_id
# and mode and leans on the schema defaults.
OPTIMIZER_COMMANDS = {
    "grid_charge_ac_priority": dict(
        mode="grid_charge", duration_minutes=20, power_percent=100,
        ac_charge_mode="ac_priority", charge_cutoff_soc=95,
    ),
    "grid_charge_pv_priority": dict(
        mode="grid_charge", duration_minutes=20, power_percent=100,
        ac_charge_mode="pv_priority", charge_cutoff_soc=95,
    ),
    "grid_charge_partial_power": dict(
        mode="grid_charge", duration_minutes=20, power_percent=45,
        ac_charge_mode="ac_priority", charge_cutoff_soc=90,
    ),
    "discharge_to_load": dict(
        mode="discharge_to_load", duration_minutes=20, power_percent=100,
        ac_charge_mode="disabled", discharge_cutoff_soc=10,
    ),
    # _resolve_discharge_mode picks discharge_to_load whenever export_rate is
    # absent OR zero, but apply_mode_with_outcome still forwards the zero. The
    # two paths reach 30200=1/30201=0 through different branches of the handler
    # and of the optimizer's prediction, so both are pinned.
    "discharge_to_load_with_explicit_zero_export": dict(
        mode="discharge_to_load", duration_minutes=20, power_percent=100,
        export_rate=0, ac_charge_mode="disabled", discharge_cutoff_soc=10,
    ),
    "discharge_to_grid_partial_export": dict(
        mode="discharge_to_grid", duration_minutes=20, power_percent=60,
        export_rate=40, ac_charge_mode="disabled", discharge_cutoff_soc=10,
    ),
    "max_export": dict(
        mode="max_export", duration_minutes=20, power_percent=100,
        export_rate=100, ac_charge_mode="disabled", discharge_cutoff_soc=10,
    ),
    "hold": dict(
        mode="hold", duration_minutes=20, power_percent=100,
        ac_charge_mode="disabled",
    ),
    "preserve_soc": dict(
        mode="preserve_soc", duration_minutes=20, power_percent=100,
        ac_charge_mode="disabled",
    ),
    "passthrough_release_control": dict(mode="passthrough"),
}


@pytest.mark.parametrize("name", sorted(OPTIMIZER_COMMANDS))
def test_the_registers_written_are_the_ones_the_optimizer_verifies_against(wired, name):
    """The whole point of register verification: write path == predicted path.

    A divergence here is silent in production — the command lands, the optimizer
    reads the registers back, disagrees with its own prediction, logs a MISMATCH,
    resends, and finally ERRORs, every slot, while the inverter is doing exactly
    what was asked.
    """
    params = OPTIMIZER_COMMANDS[name]
    _, _, _, handlers = wired()

    _set_wit_mode(handlers, device_id=DEVICE_ID, **params)

    observed = _observe(handlers)
    expected = expected_registers(params["mode"], params)
    assert expected is not None, "the optimizer has no expectation for this mode"
    assert differences_like_the_verifier(observed, expected) == [], (
        f"{name}: observed {observed} against expected {expected}"
    )


def test_a_discharge_is_written_as_a_negative_percentage(wired):
    """30409 is an unsigned word; -100 % is 65436. Comparing it raw would read
    as a 65436 % charge, which is why the optimizer decodes before deciding."""
    _, _, _, handlers = wired()
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="disabled", discharge_cutoff_soc=10)
    observed = _observe(handlers)
    assert observed[REG_REMOTE_POWER] == 65436
    assert decode_signed_power(observed[REG_REMOTE_POWER]) == -100


def test_hold_idles_the_pack_with_a_one_percent_charge_not_zero(wired):
    """30409=0 clips PV export and 30407=0 still discharges under Load First, so
    the idle command is a 1 % charge. The optimizer predicts 30409=1; a change to
    0 here would make every HOLD slot mismatch."""
    _, _, _, handlers = wired()
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="hold",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="disabled")
    observed = _observe(handlers)
    assert observed[REG_REMOTE_ENABLE] == 1
    assert observed[REG_REMOTE_POWER] == 1


def test_discharge_to_load_forces_zero_export(wired):
    """Without 30200=1/30201=0 the battery dumps to grid and the mode reads as
    Max Export. The optimizer predicts both registers for this mode with no
    export_rate in the call, so they are also what proves the slot landed."""
    _, _, _, handlers = wired()
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="disabled", discharge_cutoff_soc=10)
    observed = _observe(handlers)
    assert observed[REG_EXPORT_LIMIT_ENABLE] == 1
    assert observed[REG_EXPORT_LIMIT_RATE] == 0


def test_a_stale_export_rate_is_not_evidence_when_the_limiter_is_off(wired):
    """grid_charge clears 30200 and never touches 30201. The bank still holds the
    previous slot's 37, and the verifier's skip rule is what stops that from
    being read as a mismatch — so this asserts both halves at once."""
    _, _, _, handlers = wired()
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="ac_priority", charge_cutoff_soc=95)
    observed = _observe(handlers)
    assert observed[REG_EXPORT_LIMIT_ENABLE] == 0
    assert observed[REG_EXPORT_LIMIT_RATE] == 37, "30201 must be left untouched"
    expected = expected_registers("grid_charge", {"power_percent": 100,
                                                  "ac_charge_mode": "ac_priority"})
    assert differences_like_the_verifier(observed, expected) == []


def test_ac_priority_falling_back_to_pv_priority_is_still_a_match(wired):
    """30410=2 is rejected by some firmware and retried as 1 (#353 / the fork's
    documented fallback). The optimizer accepts {2, 1} for that register; if the
    handler ever stopped retrying, or retried with a third value, the accepted
    set would be wrong."""
    client = _client(refuse=[(30410, 2)])
    _, _, _, handlers = wired(client)
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="ac_priority", charge_cutoff_soc=95)
    observed = _observe(handlers)
    assert observed[REG_AC_CHARGE_ENABLE] == 1
    expected = expected_registers("grid_charge", {"power_percent": 100,
                                                  "ac_charge_mode": "ac_priority"})
    assert expected[REG_AC_CHARGE_ENABLE] == (2, 1)
    assert differences_like_the_verifier(observed, expected) == []


def test_30410_still_lands_when_the_device_refuses_write_single_register(wired):
    """The FC 0x06 -> FC 0x10 fallback. Before it, every other register in the
    sequence succeeded and grid charging silently never engaged."""
    client = _client(refuse_fc06=[30410])
    _, _, _, handlers = wired(client)
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="pv_priority", charge_cutoff_soc=95)
    assert _observe(handlers)[REG_AC_CHARGE_ENABLE] == 1
    assert ("fc10", 30410, [1]) in client._fake.calls


def test_back_to_back_commands_are_not_swallowed_by_the_write_rate_limiter(wired):
    """30407/30408/30409/30200/30201 carry a 30 s per-register cooldown meant to
    stop dashboard users toggling controls. The optimizer re-commands every slot
    and on every reactive recalculation, so the action clears the cooldown map
    inside the batch. Without that clear the second command returns success
    while writing nothing."""
    _, _, client, handlers = wired()
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="pv_priority", charge_cutoff_soc=95)
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                  duration_minutes=20, power_percent=80,
                  ac_charge_mode="disabled", discharge_cutoff_soc=10)
    observed = _observe(handlers)
    expected = expected_registers("discharge_to_load", {
        "power_percent": 80, "ac_charge_mode": "disabled"})
    assert differences_like_the_verifier(observed, expected) == [], (
        f"second command did not land: {observed}"
    )
    assert observed[REG_REMOTE_POWER] == 65536 - 80


def test_the_whole_sequence_runs_inside_one_write_batch(wired):
    """A half-applied command (authority granted, no setpoint) is worse than one
    that plainly failed, and the optimizer cannot tell the two apart: it only
    sees success. The batch must be open for the first write and still open for
    the last."""
    _, _, client, handlers = wired()
    seen = []
    real_batch = client.write_batch

    @contextmanager
    def _spy(what="write sequence", timeout=None):
        with real_batch(what, timeout=timeout):
            seen.append(("enter", what, len(client._fake.calls)))
            yield
            seen.append(("exit", what, len(client._fake.calls)))

    client.write_batch = _spy
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="pv_priority", charge_cutoff_soc=95)

    assert [s[0] for s in seen] == ["enter", "exit"], "one batch, not one per write"
    assert seen[0][1] == "set_wit_mode -> grid_charge"
    assert seen[0][2] == 0, "writes started before the batch was entered"
    assert seen[1][2] >= 8, "writes continued after the batch was released"


# ---------------------------------------------------------------------------
# (b) Response shape and error reporting
# ---------------------------------------------------------------------------


def test_the_success_response_carries_the_keys_direct_control_reads(wired):
    """DirectControl._call_set_wit_mode reads `success` and nothing else, but it
    reads it out of whatever envelope AppDaemon hands back, so `success` must be
    a top-level bool. The other four keys are the documented response and are
    logged by the caller."""
    _, coordinator, _, handlers = wired()
    response = _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                             duration_minutes=20, power_percent=100,
                             ac_charge_mode="pv_priority", charge_cutoff_soc=95)
    assert isinstance(response, dict)
    assert response["success"] is True
    assert response["mode_applied"] == "grid_charge"
    assert set(response) == {"success", "mode_applied", "registers_written",
                             "timestamp", "override_expires"}
    # JSON-serialisable: this crosses a websocket before the optimizer sees it.
    assert all(isinstance(k, str) for k in response["registers_written"])
    assert coordinator.refreshed == 1


def test_a_refused_register_write_raises_rather_than_reporting_success(wired):
    """DirectControl treats a raised call as a CONFIRMED failure: it does not
    record the last-sent marker, so the next slot resends instead of being
    suppressed as a duplicate. Reporting success=True here would strand the
    inverter in the previous mode with the optimizer believing otherwise."""
    client = _client(refuse=[(30409, 65436)])
    _, _, _, handlers = wired(client)
    with pytest.raises(Exception) as excinfo:
        _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                      duration_minutes=20, power_percent=100,
                      ac_charge_mode="disabled", discharge_cutoff_soc=10)
    assert "set_wit_mode failed" in str(excinfo.value)


def test_a_bus_lock_timeout_reaches_the_caller_as_a_failure(wired):
    """Under write_batch a busy bus surfaces as ModbusWriteError. It must not
    escape unclassified or, worse, be swallowed into a success response."""
    _, _, client, handlers = wired()

    def _busy(what="write sequence", timeout=None):
        raise _gm.ModbusWriteError(0, [], f"Modbus bus busy (lock timeout on {what})")

    client.write_batch = _busy
    with pytest.raises(Exception) as excinfo:
        _set_wit_mode(handlers, device_id=DEVICE_ID, mode="hold",
                      duration_minutes=20, power_percent=100,
                      ac_charge_mode="disabled")
    assert "set_wit_mode failed" in str(excinfo.value)


def test_an_unknown_device_raises_before_touching_the_bus(wired):
    _, _, client, handlers = wired()
    before = len(client._fake.calls)
    with pytest.raises(ValueError):
        _set_wit_mode(handlers, device_id="not-a-device", mode="hold",
                      duration_minutes=20, power_percent=100)
    assert len(client._fake.calls) == before


def test_a_profile_without_the_vpp_block_is_refused_with_a_user_visible_error(wired):
    """Register-presence gate, not a model-family gate (#373). HomeAssistantError
    rather than ValueError so the message reaches the UI; the optimizer records
    it as a confirmed failure either way, which is right for an unsupported
    profile."""
    from homeassistant.exceptions import HomeAssistantError

    client = _gm.GrowattModbus(connection_type="tcp", host="10.0.0.1", port=502,
                               register_map="SPF_3000_6000_ES_PLUS")
    client.client = _FakeModbus()
    _, _, _, handlers = wired(client)
    with pytest.raises(HomeAssistantError):
        _set_wit_mode(handlers, device_id=DEVICE_ID, mode="hold",
                      duration_minutes=20, power_percent=100)


def test_the_wit_profile_carries_every_register_the_gate_demands(wired):
    """The other half of the gate: it must not lock out the one profile this
    installation runs. A missing entry would raise HomeAssistantError on every
    single slot."""
    _, _, client, _ = wired()
    holding = client.register_map.get("holding_registers", {})
    for reg in (30100, 30200, 30201, 30404, 30405,
                30407, 30408, 30409, 30410, 30411, 30476):
        assert reg in holding, f"WIT profile is missing holding register {reg}"


# ---------------------------------------------------------------------------
# (c) get_register_data — the verification read
# ---------------------------------------------------------------------------


def test_get_register_data_returns_a_values_list_of_exactly_count(wired):
    """DirectControl treats a short or missing `values` list as UNVERIFIABLE, so
    a truncated read is safe but useless — verification silently stops working
    while the counters call it a read failure."""
    _, _, _, handlers = wired()
    for start, count in (REG_BLOCK_CONTROL, REG_BLOCK_EXPORT):
        result = _get_register_data(handlers, start, count)
        assert result["success"] is True
        assert isinstance(result["values"], list)
        assert len(result["values"]) == count
        assert all(isinstance(v, int) for v in result["values"])


def test_get_register_data_bypasses_the_optional_holding_blacklist(wired):
    """The reason the optimizer verifies through registers instead of through
    sensor.growatt_inverter_mode. The poll path backs a VPP block off after
    repeated failures and the derived sensor then reports Unknown; this action
    goes straight to read_holding_registers, so verification keeps working
    through a backoff window."""
    import time as _time

    _, _, client, handlers = wired()
    now = _time.time()
    for anchor in (30100, 30200, 30407):
        client._failed_optional_holding_addrs[anchor] = (now, 99)

    assert client._optional_holding_blocked(30407) is True
    assert client._optional_holding_blocked(30200) is True

    control = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    export = _get_register_data(handlers, *REG_BLOCK_EXPORT)
    assert control["success"] is True and control["values"][0] == STALE_BANK[30407]
    assert export["success"] is True and export["values"][1] == STALE_BANK[30201]


def test_get_register_data_reports_a_failed_read_rather_than_zeros(wired):
    """A read that could not be taken must not come back as a block of zeros:
    30407=0 decodes to "Passthrough", which is exactly the false mismatch this
    verification path was introduced to end."""
    _, _, client, handlers = wired()

    def _dead(address=None, count=None, **kwargs):
        return None

    client.client.read_holding_registers = _dead
    result = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    assert result["success"] is False
    assert result["values"] == []
    assert result.get("error")


def test_get_register_data_reads_holding_when_asked_for_holding(wired):
    """register_type is not cosmetic: input 30407 is a different register."""
    _, _, client, handlers = wired()
    seen = []
    client.read_input_registers = lambda **kw: seen.append(("input", kw)) or [0, 0, 0, 0]
    _get_register_data(handlers, *REG_BLOCK_CONTROL)
    assert seen == [], "a holding read was served from the input space"
    assert ("fc03", 30407, [4]) in client._fake.calls


# ---------------------------------------------------------------------------
# Schema and registration — the call the optimizer actually makes
# ---------------------------------------------------------------------------


def _schema_source(name: str) -> str:
    """The literal body of a module-level vol.Schema(...).

    Read from source rather than introspected: voluptuous is stubbed in this
    suite (conftest replaces it when the real package is absent), so the schema
    objects carry no usable structure. The keys are what matter here anyway —
    they are the wire names AppDaemon puts in the service call.

    Both brace styles in this file are accepted: `vol.Schema({ ... })` and
    `vol.Schema(\n    { ... }\n)`.
    """
    match = re.search(rf"^{name} = vol\.Schema\((.*?)^\}}?\)", DIAGNOSTIC_SRC,
                      re.S | re.M)
    assert match, f"{name} is no longer a module-level vol.Schema"
    return match.group(1)


def test_the_set_wit_mode_schema_accepts_every_parameter_the_optimizer_sends():
    """voluptuous is strict: an accepted key that is dropped from the schema
    fails the call with "extra keys not allowed", every slot, immediately."""
    source = _schema_source("SERVICE_SET_WIT_MODE_SCHEMA")
    for key in ("device_id", "mode", "power_percent", "duration_minutes",
                "export_rate", "ac_charge_mode",
                "charge_cutoff_soc", "discharge_cutoff_soc"):
        assert f'"{key}"' in source, f"set_wit_mode no longer accepts {key}"
    # release_control() sends only device_id and mode.
    assert 'vol.Optional("power_percent", default=100)' in source
    assert 'vol.Optional("duration_minutes", default=60)' in source


def test_the_get_register_data_schema_names_the_field_start_address():
    """The optimizer sends start_address. `address` is what the neighbouring
    write_register action calls it, and confusing the two makes every
    verification fail schema validation."""
    source = _schema_source("SERVICE_GET_REGISTER_DATA_SCHEMA")
    for key in ("device_id", "register_type", "start_address", "count"):
        assert f'"{key}"' in source
    assert '"address"' not in source
    # Both verification blocks must be inside the count range.
    assert "max=50" in source


def test_every_mode_the_optimizer_can_send_is_a_valid_choice():
    for mode in ("grid_charge", "discharge_to_load", "discharge_to_grid",
                 "max_export", "hold", "passthrough", "preserve_soc"):
        assert mode in _diag.WIT_MODE_CHOICES


def test_the_ac_charge_mode_values_match_the_optimizers_table():
    """30410 is compared numerically by the verifier, so the name -> value map is
    part of the contract, not an implementation detail."""
    assert _diag.AC_CHARGE_MODE_MAP == AC_CHARGE_MODE_VALUES


def test_both_actions_are_registered_with_a_response(wired):
    """AppDaemon only surfaces a service response for SupportsResponse.OPTIONAL /
    ONLY. Registered without it, get_register_data returns nothing and every
    verification becomes UNVERIFIABLE while looking like a read failure."""
    _, _, _, handlers = wired()
    for service in ("set_wit_mode", "get_register_data"):
        assert handlers[service]["domain"] == "growatt_modbus"
        assert handlers[service]["supports_response"] is _diag.SupportsResponse.OPTIONAL


# ---------------------------------------------------------------------------
# (e) Both actions must fail inside the caller's deadline, not after it
# ---------------------------------------------------------------------------

# battery_optimizer's DirectControl passes hass_timeout=15 to both actions
# (SET_WIT_MODE_TIMEOUT_SECONDS, reused by RegisterVerifier). Anything the handler does
# after that lands on nobody: the optimizer has already recorded an unconfirmed timeout
# and released its own I/O lock.
OPTIMIZER_DEADLINE_S = 15


def test_the_service_bus_wait_is_shorter_than_the_optimizers_deadline():
    """#398 made every read and write queue behind a whole poll, at SHARED_LOCK_TIMEOUT.

    That is a poll-sized wait (60 s) on a call whose caller gives up after 15 s. A service
    call has to fail INSIDE the window instead: for the batch that means nothing was
    written, which is a confirmed failure the optimizer retries; for the verify read it
    means UNVERIFIABLE rather than an answer produced long after the question expired.
    """
    assert _const.SERVICE_BUS_TIMEOUT < OPTIMIZER_DEADLINE_S
    assert _const.SERVICE_BUS_TIMEOUT < _const.SHARED_LOCK_TIMEOUT


def test_set_wit_mode_takes_the_bus_with_the_service_timeout(wired):
    _, _, client, handlers = wired()
    seen = []
    real_batch = client.write_batch

    @contextmanager
    def _spy(what="write sequence", timeout=None):
        seen.append(timeout)
        with real_batch(what, timeout=timeout):
            yield

    client.write_batch = _spy
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="hold",
                  duration_minutes=20, power_percent=100)
    assert seen == [_const.SERVICE_BUS_TIMEOUT]


def test_get_register_data_takes_the_bus_with_the_service_timeout(wired):
    _, _, client, handlers = wired()
    seen = []
    real_read = client.read_holding_registers

    def _spy(start_address=None, count=None, bus_timeout=None):
        seen.append(bus_timeout)
        return real_read(start_address, count)

    client.read_holding_registers = _spy
    assert _get_register_data(handlers, *REG_BLOCK_CONTROL)["success"] is True
    assert seen == [_const.SERVICE_BUS_TIMEOUT]


def test_a_busy_bus_makes_the_verify_read_unverifiable_not_an_exception(wired):
    """DirectControl's RegisterVerifier reads {"success", "values"} and treats an unclean
    read as UNVERIFIABLE - never as a MISMATCH. A ModbusWriteError from the bus lock has
    to arrive in that shape, not as a raised service call, which the caller would classify
    as a confirmed failure of a read that changed nothing."""
    _, _, client, handlers = wired()

    def _busy(start_address=None, count=None, bus_timeout=None):
        raise _gm.ModbusWriteError(0, [], "Modbus bus busy (lock timeout after 10s on read)")

    client.read_holding_registers = _busy
    result = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    assert result["success"] is False
    assert result["values"] == []
    assert "busy" in result["error"]


# ---------------------------------------------------------------------------
# (f) 30410 pays the FC 0x06 probe once, not once per schedule slot
# ---------------------------------------------------------------------------


def test_the_fc06_probe_on_30410_is_paid_once_not_on_every_command(wired):
    """The optimizer sends a mode command every 15-minute slot, and every mode writes
    30410. On firmware that refuses FC 0x06 for it, an unconditional probe is a rejected
    transaction with the bus held plus a WARNING per slot - tens a day for a condition
    that is known, expected and handled.
    """
    client = _client(refuse_fc06=[30410])
    _, _, _, handlers = wired(client)

    def _fc_calls():
        return [c[0] for c in client._fake.calls if c[1] == 30410]

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100)
    first = _fc_calls()
    assert first == ["fc06", "fc10"], first
    assert 30410 in client._fc10_only_registers

    client._fake.calls.clear()
    for _ in range(3):
        _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                      duration_minutes=20, power_percent=100)
    later = _fc_calls()
    assert later == ["fc10", "fc10", "fc10"], later
    assert client._fake.registers[30410] == 1


def test_a_refused_register_is_not_blamed_on_the_rate_limiter(wired, caplog):
    """`_write` reported a falsy return and a raised write differently: only the raised
    path backed off and named the device's own reason, while a False fell through to
    "(rate limited?)" - a cause the batch clears at the top with
    `_wit_control_last_write.clear()`. Routing 30410 through write_single_register_any_fc,
    which swallows the ModbusWriteError and returns False, made that the normal path for
    the one register most likely to be refused.
    """
    client = _client(refuse=[(30410, 0)], refuse_fc06=[30410])
    _, _, _, handlers = wired(client)

    with caplog.at_level("DEBUG"):
        with pytest.raises(ValueError):
            _set_wit_mode(handlers, device_id=DEVICE_ID, mode="hold",
                          duration_minutes=20, power_percent=100)

    messages = [r.getMessage() for r in caplog.records]
    assert not any("rate limited?" in m for m in messages), messages
    assert any("Register 30410 write failed after 2 attempts" in m for m in messages), messages
    # Both attempts were made, i.e. the refusal did not short-circuit the retry.
    # Two attempts, each probing both function codes: the memo is only set by a
    # SUCCESSFUL FC 0x10, so a register refused on both is not silently downgraded.
    assert len([c for c in client._fake.calls if c[1] == 30410 and c[0] == "fc06"]) == 2
    assert len([c for c in client._fake.calls if c[1] == 30410 and c[0] == "fc10"]) == 2


# ---------------------------------------------------------------------------
# (g) The gate must list every register the sequence raises on
# ---------------------------------------------------------------------------

# Registers set_wit_mode writes unconditionally-or-nearly and RAISES on, i.e. a profile
# without them passes the gate and then aborts mid-sequence, after 30100=1 has already
# granted control authority with nothing behind it.
HARD_REQUIRED = (30200, 30201, 30407, 30409, 30410, 30411, 30476)


def _handler_source(name: str) -> str:
    """The literal body of a handler defined inside async_setup_services."""
    tree = ast.parse(DIAGNOSTIC_SRC)
    func = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    return ast.get_source_segment(DIAGNOSTIC_SRC, func)


@pytest.mark.parametrize("register", HARD_REQUIRED)
def test_the_gate_names_every_register_the_sequence_raises_on(register):
    body = _handler_source("set_wit_mode")
    gate = body[:body.index("def _apply()")]
    assert str(register) in gate, (
        f"set_wit_mode raises when {register} cannot be written but does not gate on it, "
        f"so an unsupported profile aborts mid-sequence instead of being refused"
    )


def test_the_profiles_the_gate_now_refuses_would_have_aborted_mid_sequence():
    """Three shipped profiles carry 30407/30409/30410 without 30411 or 30476. Under the
    narrower gate they were accepted and then failed at the priority-mode write - with
    30100=1 already applied."""
    partial = sorted(
        name for name, m in _const.REGISTER_MAPS.items()
        if all(r in m.get("holding_registers", {}) for r in (30407, 30409, 30410))
        and not all(r in m.get("holding_registers", {}) for r in HARD_REQUIRED)
    )
    assert partial == [
        "MOD_6000_15000TL3_XH", "SPH_3000_6000_V201", "SPH_7000_10000_V201",
    ], partial


def test_both_wit_profiles_still_pass_the_gate():
    """A gate tight enough to exclude the hardware this action exists for is a silent
    removal of the feature."""
    for name in _const.WIT_REGISTER_MAPS:
        holding = _const.REGISTER_MAPS[name].get("holding_registers", {})
        missing = [r for r in HARD_REQUIRED if r not in holding]
        assert not missing, f"{name} would be refused: missing {missing}"


# ---------------------------------------------------------------------------
# (h) The shared-connection path — the one the reference installation runs
# ---------------------------------------------------------------------------
#
# Everything above drives a `GrowattModbus` with `_shared_conn = None`. That is not
# what runs in production: `__init__.py` creates a `SharedModbusConnection` for every
# TCP entry (a hub is built even for a single one) and `coordinator.py` passes it as
# `shared_conn=`, so the reference WIT takes the shared branch of every read and write.
#
# The two branches are genuinely different code, not a wrapper:
#   * `write_batch` holds `hub._lock` instead of `_local_bus_lock`, and each write
#     inside then re-acquires that same RLock — reentrant only because it is the same
#     thread, which is exactly the constraint `_apply`'s docstring rests on.
#   * `SharedModbusConnection.write_register` returns a bool where the direct path
#     returns a pymodbus response, and `_write_register_locked` turns a False into a
#     raised `ModbusWriteError`. The handler's `_write` has to classify both.
#   * reads go through `_validate_registers`, which the direct path does not have.
#
# So the register sequence the optimizer verifies against is re-proved here on the
# branch that actually executes, and the handler is dispatched on a real worker thread
# rather than inline, because per-thread RLock reentrancy is the whole reason the
# sequence must not be split across executor jobs.


class _ThreadedHass(_Hass):
    """Runs the executor job on a different thread, as Home Assistant does.

    Inline execution would hide a split-sequence deadlock: the batch lock is an RLock,
    so a second acquisition succeeds on the calling thread and models nothing.
    """

    async def async_add_executor_job(self, func, *args):
        box = {}

        def _run():
            try:
                box["value"] = func(*args)
            except BaseException as err:  # noqa: BLE001
                box["error"] = err

        thread = threading.Thread(target=_run, name="fake-executor")
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "the register sequence deadlocked against itself"
        if "error" in box:
            raise box["error"]
        return box["value"]


@pytest.fixture
def wired_shared(monkeypatch):
    """(hass, coordinator, client, handlers) with a real SharedModbusConnection."""

    def _build(seed=None, refuse=(), refuse_fc06=()):
        fake = _FakeModbus(
            seed=STALE_BANK if seed is None else seed,
            refuse=refuse,
            refuse_fc06=refuse_fc06,
        )
        monkeypatch.setattr(_gm, "ModbusTcpClient", lambda *a, **k: fake)
        hub = _gm.SharedModbusConnection(host="10.0.0.1", port=502)
        assert hub.ensure_connected(), "the hub refused to connect to the fake client"
        client = _gm.GrowattModbus(
            connection_type="tcp", host="10.0.0.1", port=502,
            register_map=WIT_MAP, shared_conn=hub,
        )
        client.min_read_interval = 0
        client._default_min_read_interval = 0
        client._fake = fake
        coordinator = _Coordinator(client)
        hass = _ThreadedHass(coordinator)
        monkeypatch.setattr(_diag, "dr", _FakeDrModule)
        asyncio.run(_diag.async_setup_services(hass))
        return hass, coordinator, client, hass.registered

    return _build


@pytest.mark.parametrize("name", sorted(OPTIMIZER_COMMANDS))
def test_the_shared_connection_writes_the_same_registers(wired_shared, name):
    """Same assertion as the direct-path case, on the branch production takes."""
    params = OPTIMIZER_COMMANDS[name]
    _, _, _, handlers = wired_shared()

    _set_wit_mode(handlers, device_id=DEVICE_ID, **params)

    observed = _observe(handlers)
    expected = expected_registers(params["mode"], params)
    assert differences_like_the_verifier(observed, expected) == [], (
        f"{name}: observed {observed} against expected {expected}"
    )


def test_the_shared_sequence_is_one_batch_on_one_thread(wired_shared):
    """The bus is taken once, before the first write, and released after the last.

    On the shared branch the lock held is the hub's — the same one a poll takes for its
    whole duration — so a leak here stalls polling for every entity on the connection,
    and a split sequence would deadlock rather than interleave.
    """
    _, _, client, handlers = wired_shared()
    hub = client._shared_conn
    assert hub is not None, "the fixture did not build a shared connection"

    depth = []
    real_batch = client.write_batch

    @contextmanager
    def _spy(what="write sequence", timeout=None):
        with real_batch(what, timeout=timeout):
            depth.append(("enter", len(client._fake.calls)))
            yield
            depth.append(("exit", len(client._fake.calls)))

    client.write_batch = _spy
    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="disabled", discharge_cutoff_soc=10)

    assert [d[0] for d in depth] == ["enter", "exit"]
    assert depth[0][1] == 0 and depth[1][1] >= 8
    # Released, not leaked: a poll must be able to take it straight afterwards.
    assert hub._lock.acquire(timeout=0) is True
    hub._lock.release()


def test_the_fc06_refusal_on_30410_is_memoised_on_the_shared_path_too(wired_shared):
    """30410 rejects FC 0x06 on the reference firmware (#353). The fallback must be
    learned once, not re-probed every slot — the shared bus is the contended one."""
    _, _, client, handlers = wired_shared(refuse_fc06=(30410,))

    def _fc06_probes():
        return [c for c in client._fake.calls if c[0] == "fc06" and c[1] == 30410]

    charge = dict(mode="grid_charge", duration_minutes=20, power_percent=100,
                  ac_charge_mode="pv_priority", charge_cutoff_soc=95)
    _set_wit_mode(handlers, device_id=DEVICE_ID, **charge)
    assert len(_fc06_probes()) == 1
    assert 30410 in client._fc10_only_registers

    for _ in range(3):
        _set_wit_mode(handlers, device_id=DEVICE_ID, **charge)
    assert len(_fc06_probes()) == 1, "the known-refused FC 0x06 probe was paid again"
    assert _observe(handlers)[REG_AC_CHARGE_ENABLE] == 1


def test_a_short_frame_is_a_failed_read_not_a_truncated_values_list(wired_shared):
    """A misaligned gateway frame must read as UNVERIFIABLE, never as evidence.

    `values` is consumed positionally: values[0] is 30407 and values[2] is the power
    word. A three-register answer to a count-4 request would put the AC charge mode
    where the power word belongs, and the mode decoded from it is one nobody
    commanded. `_validate_registers` discards the frame, so the action reports
    success=False and DirectControl records UNVERIFIABLE (#367).
    """
    _, _, client, handlers = wired_shared()

    def _short(address=None, count=None, **kwargs):
        return _Read([1, 20, 65436][:max(1, count - 1)])

    client._fake.read_holding_registers = _short
    result = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    assert result["success"] is False
    assert result["values"] == []


def test_a_read_the_bus_cannot_take_is_reported_rather_than_raised(wired_shared):
    """DirectControl maps a raised call and success=False to the same UNVERIFIABLE, but
    only because the action never lets a bus exception escape as a service error."""
    _, _, client, handlers = wired_shared()

    def _boom(**kwargs):
        raise RuntimeError("bus wedged")

    client.read_holding_registers = _boom
    result = _get_register_data(handlers, *REG_BLOCK_CONTROL)
    assert result["success"] is False
    assert result["values"] == []
    assert "bus wedged" in str(result.get("error"))


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_every_offered_mode_writes_all_four_verified_registers(wired, mode):
    """No mode may reach the end of the sequence having skipped a step.

    Each of steps 2 (30410), 4 (30200) and 5 (30407/30409) is an if/elif chain over
    literal mode names with no else, so a mode string that is in `const.WIT_MODES` -
    and therefore accepted by the schema - but missing from one of those tuples writes
    only 30100/30476/30411 and still returns success=True. The optimizer would then
    read registers that match nothing it predicted: MISMATCH, resend, ERROR, every
    slot, with the action reporting success throughout. That is one const.py line away,
    so the coverage is asserted from the mode list itself rather than from the
    hand-written command table above.
    """
    _, _, _, handlers = wired()
    response = _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                             duration_minutes=20, power_percent=100)
    written = {int(k) for k in response["registers_written"]}
    missing = [r for r in (REG_AC_CHARGE_ENABLE, REG_EXPORT_LIMIT_ENABLE,
                           REG_REMOTE_ENABLE, REG_REMOTE_POWER) if r not in written]
    assert not missing, (
        f"mode '{mode}' reported success without writing {missing}; "
        f"it is missing from a step's mode tuple in set_wit_mode"
    )


# ---------------------------------------------------------------------------
# (i) `registers_written` records what landed, not what was attempted
# ---------------------------------------------------------------------------

# Every register the sequence touches, pre-seeded with a value no command in this
# module produces. A register recorded in the response but never actually written
# therefore reads back as 4242, so `bank[reg] == recorded` can only hold for a
# write that happened — the STALE_BANK seed would let 30407=1 pass on a stale 1.
UNWRITTEN = 4242
UNWRITTEN_BANK = {
    reg: UNWRITTEN
    for reg in (30100, 30200, 30201, 30404, 30405,
                30407, 30408, 30409, 30410, 30411, 30476)
}

REG_CONTROL_AUTHORITY = 30100


def test_a_refused_control_authority_is_not_reported_as_written(wired):
    """30100 is the one step that warns and carries on instead of raising, and its
    record was written unconditionally — so the response claimed 30100=1 for a write
    the inverter had just refused.

    `registers_written` is the only machine-readable account of what the action did,
    and 30100 is the register whose absence makes 30407 a no-op on some profiles. A
    slot that quietly failed to charge would then be diagnosed from a response saying
    control authority was granted.
    """
    client = _client(seed=dict(UNWRITTEN_BANK), refuse=[(REG_CONTROL_AUTHORITY, 1)])
    _, _, _, handlers = wired(client)

    response = _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                             duration_minutes=20, power_percent=100,
                             ac_charge_mode="ac_priority")

    # The warn-and-continue is intact: the rest of the sequence still ran and the
    # action still reports success. Only the false record is gone.
    assert response["success"] is True
    assert client._fake.registers[REG_CONTROL_AUTHORITY] == UNWRITTEN, (
        "the device refused the write; the bank must be untouched"
    )
    assert str(REG_CONTROL_AUTHORITY) not in response["registers_written"], (
        "30100 was reported as written after the device refused it"
    )
    # Omission is the whole signal, so the steps that did land must still be there.
    for reg in (30476, REG_AC_CHARGE_ENABLE, REG_REMOTE_ENABLE, REG_REMOTE_POWER):
        assert str(reg) in response["registers_written"], reg


def test_control_authority_is_reported_when_it_lands(wired):
    """The other half: omission only means "refused" while a successful write is
    always recorded."""
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    response = _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                             duration_minutes=20, power_percent=100)

    assert response["registers_written"][str(REG_CONTROL_AUTHORITY)] == 1
    assert client._fake.registers[REG_CONTROL_AUTHORITY] == 1


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_every_reported_register_actually_reached_the_device(wired, mode):
    """The general invariant, asserted from the mode list rather than for 30100 alone:
    nothing may appear in `registers_written` that the bank did not receive.

    Every step except 30100 raises on failure, so today they cannot lie — but that is
    a property of each `if not success: raise`, and the next warn-and-continue step
    added would silently reintroduce the same defect.
    """
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    response = _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                             duration_minutes=20, power_percent=100,
                             ac_charge_mode="ac_priority", charge_cutoff_soc=95,
                             discharge_cutoff_soc=10)

    claimed = {int(k): v for k, v in response["registers_written"].items()}
    lied = {
        reg: (val, client._fake.registers.get(reg))
        for reg, val in claimed.items()
        if client._fake.registers.get(reg) != val
    }
    assert not lied, (
        f"mode '{mode}' reported registers it did not write "
        f"(register: (claimed, actual)): {lied}"
    )


# ---------------------------------------------------------------------------
# (j) The registers the optimizer CANNOT verify
# ---------------------------------------------------------------------------

# RegisterVerifier reads exactly two blocks - 30407+4 and 30200+2 - because each
# read is a blocking service call on the AppDaemon callback thread and 30100 and
# 30476 "neither change the verdict" (direct_control.py, RegisterVerifier
# docstring). That is a sound trade, but it means the sequence writes four
# registers no consumer ever checks, and each has the same failure shape: the
# verified six read back exactly as predicted, the verification passes, and the
# battery does not do what the schedule says.
#
#   30476 = 1 (Battery First) is REQUIRED for grid charging. With 0 (Load First)
#           or 2 (Grid First) the inverter accepts 30407=1 / 30409=+100 and
#           charges at 0 W - confirmed on WIT 8000TL3-HU V1.39, and the reason
#           profiles/wit.py marks the register RW at all.
#   30411 = 0 clears any leftover TOU period. A stale period drives the battery
#           on its own schedule against the override; it is exactly what the
#           fork removed GrowattWitVppBatteryModeSelect's Hold path for.
#   30100 = 1 grants VPP control authority, without which 30407 is a no-op on
#           some profiles.
#   30404/30405 are the SOC cutoffs. Nothing reads them back into GrowattData
#           either, so the mode sensor's cutoff attributes report dataclass
#           defaults - the registers themselves are only ever observable here.
#
# So this section is the only place any of them is checked at all.

VPP_PRIORITY_MODE = 30476
VPP_TOU_NUM_PERIODS = 30411
VPP_CHARGE_CUTOFF_SOC = 30404
VPP_DISCHARGE_CUTOFF_SOC = 30405


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_battery_first_is_written_for_every_mode_that_moves_the_battery(wired, mode):
    """30476: 1 for everything except passthrough, which releases to Load First.

    Bank seeded with a value no command produces, so a pass cannot come from a
    leftover 1 - which is what the register would hold on a real inverter for as
    long as the schedule kept working, hiding the regression until the first
    restart or manual write.
    """
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                  duration_minutes=20, power_percent=100)

    expected = 0 if mode == "passthrough" else 1
    assert client._fake.registers[VPP_PRIORITY_MODE] == expected, (
        f"mode '{mode}' left 30476 at "
        f"{client._fake.registers[VPP_PRIORITY_MODE]}, not {expected}; a "
        f"grid_charge slot would verify perfectly and charge at 0 W"
    )


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_leftover_tou_periods_are_cleared_for_every_mode(wired, mode):
    """30411=0 before the power command, for every mode without exception."""
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                  duration_minutes=20, power_percent=100)

    assert client._fake.registers[VPP_TOU_NUM_PERIODS] == 0, (
        f"mode '{mode}' did not clear 30411; a stale TOU period runs the "
        f"battery on its own schedule underneath the override"
    )


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_control_authority_is_granted_for_every_mode(wired, mode):
    """30100=1 is attempted for every mode. It is the one warn-and-continue
    step, so this asserts the device received it, not that the action insisted
    on it."""
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                  duration_minutes=20, power_percent=100)

    assert client._fake.registers[REG_CONTROL_AUTHORITY] == 1, mode


def test_the_soc_cutoffs_reach_their_registers_unscaled(wired):
    """30404/30405 carry the optimizer's max_soc / min_soc verbatim.

    They are write-only in this integration - no poll reads them back - so a
    scaling or swap error here is invisible everywhere else. Sent on different
    calls because the optimizer sends the charge cutoff only for CHARGE slots
    and the discharge cutoff only for DISCHARGE slots.
    """
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="grid_charge",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="ac_priority", charge_cutoff_soc=95)
    assert client._fake.registers[VPP_CHARGE_CUTOFF_SOC] == 95
    assert client._fake.registers[VPP_DISCHARGE_CUTOFF_SOC] == UNWRITTEN, (
        "a charge slot sends no discharge cutoff and must not invent one"
    )

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode="discharge_to_load",
                  duration_minutes=20, power_percent=100,
                  ac_charge_mode="disabled", discharge_cutoff_soc=15)
    assert client._fake.registers[VPP_DISCHARGE_CUTOFF_SOC] == 15


@pytest.mark.parametrize("mode", sorted(_const.WIT_MODES))
def test_the_duration_the_optimizer_sends_reaches_30408(wired, mode):
    """30408 is read by the verifier but deliberately NOT compared (it does not
    count down), so nothing on either side would notice it being dropped. It is
    still the override's dead-man switch: the inverter releases the override
    when it expires, and the optimizer sends slot_minutes + buffer for exactly
    that reason."""
    client = _client(seed=dict(UNWRITTEN_BANK))
    _, _, _, handlers = wired(client)

    _set_wit_mode(handlers, device_id=DEVICE_ID, mode=mode,
                  duration_minutes=20, power_percent=100)

    expected = 0 if mode == "passthrough" else 20
    assert client._fake.registers[REG_REMOTE_DURATION] == expected, mode
