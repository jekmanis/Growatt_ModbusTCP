"""A single-register write must survive hardware that only accepts FC 0x10 (#353).

Register 30410 (VPP AC charge enable) rejects Write Single Register on at least one WIT
8000TL3-HU and accepts Write Multiple Registers with count=1. The caller logged a warning
and continued, so every other write in the mode sequence succeeded and grid charging
silently never engaged - the control reported success and did nothing.

FC 0x06 is still tried first. Switching everything to FC 0x10 would risk the opposite
failure on devices that only accept the single-register form, and there is one report in
each direction. The fallback can only add an attempt where the first was refused outright.
"""
from __future__ import annotations

import importlib

import pytest

_gm = importlib.import_module("growatt_under_test.growatt_modbus")

REG = 30410


class _Client(_gm.GrowattModbus):
    """Records which function codes were attempted, and how each was answered."""

    def __init__(self, single_ok=True, multi_ok=True, single_raises=False):
        super().__init__(connection_type="tcp", host="10.0.0.1", port=502,
                         register_map="WIT_4000_15000TL3")
        self.calls: list[str] = []
        self._single_ok = single_ok
        self._multi_ok = multi_ok
        self._single_raises = single_raises

    def write_register(self, register, value):
        self.calls.append("fc06")
        if self._single_raises:
            raise OSError("Illegal Function")
        return self._single_ok

    def write_registers(self, register, values):
        self.calls.append("fc10")
        return self._multi_ok


def test_fc06_is_tried_first_and_nothing_else_happens_when_it_works():
    """The common path must be untouched - no extra traffic on healthy hardware."""
    c = _Client()
    assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc06"], "FC 0x10 was attempted even though FC 0x06 succeeded"


def test_it_falls_back_when_fc06_raises():
    """The reported case: the device refuses Write Single Register outright."""
    c = _Client(single_raises=True, multi_ok=True)
    assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc06", "fc10"]


def test_it_falls_back_when_fc06_returns_false():
    """Not every refusal raises - some paths report failure by return value."""
    c = _Client(single_ok=False, multi_ok=True)
    assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc06", "fc10"]


def test_both_refused_reports_failure():
    """The caller must be able to tell the difference between 'worked somehow' and
    'nothing worked' - the old code could not, which is why this went unnoticed."""
    c = _Client(single_ok=False, multi_ok=False)
    assert c.write_single_register_any_fc(REG, 1) is False
    assert c.calls == ["fc06", "fc10"]


def test_a_raising_fc10_is_not_propagated():
    """A mode change must not abort on the fallback attempt itself."""
    c = _Client(single_raises=True)
    c.write_registers = lambda r, v: (_ for _ in ()).throw(OSError("also refused"))
    assert c.write_single_register_any_fc(REG, 1) is False


def test_the_vpp_charge_paths_use_it():
    """Both the Hold and Charge sequences write 30410. A helper wired into one of them
    would leave the other silently broken."""
    from pathlib import Path

    source = (Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
              / "select.py").read_text(encoding="utf-8")
    assert source.count("write_single_register_any_fc(self.VPP_AC_CHARGE_ENABLE, 1)") == 2, (
        "not both 30410 write sites go through the fallback"
    )
    assert "client.write_register(self.VPP_AC_CHARGE_ENABLE" not in source, (
        "a bare FC 0x06 write to 30410 remains"
    )


def test_a_refusal_is_remembered_so_fc06_is_probed_once_per_register():
    """The probe is cheap once and expensive every time.

    30410 is written by every `set_wit_mode` call - once per 15-minute schedule slot on
    the reference installation. On firmware that refuses FC 0x06 for it, an unconditional
    probe is a rejected Modbus transaction with the bus held, plus a WARNING from the write
    path, on every command: tens a day for a condition that is known, expected and already
    handled. This project's logging rule is warn once, then be quiet.
    """
    c = _Client(single_ok=False)
    assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc06", "fc10"]
    assert REG in c._fc10_only_registers

    c.calls.clear()
    for _ in range(5):
        assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc10"] * 5, "FC 0x06 was probed again after a known refusal"


def test_the_memo_is_per_register():
    """Another register must not inherit 30410's verdict."""
    c = _Client(single_ok=False)
    c.write_single_register_any_fc(REG, 1)
    c.calls.clear()
    c._single_ok = True
    assert c.write_single_register_any_fc(30407, 1) is True
    assert c.calls == ["fc06"]


def test_a_later_fc06_success_clears_the_memo():
    """Nothing here is authoritative about firmware. If FC 0x06 starts working - a
    firmware update, or a different unit behind the same client - the memo must not
    keep the register on the slower path forever."""
    c = _Client(single_ok=False)
    c.write_single_register_any_fc(REG, 1)
    assert REG in c._fc10_only_registers

    c._fc10_only_registers.discard(REG)  # as a reconnect/rebuild would
    c._single_ok = True
    c.calls.clear()
    assert c.write_single_register_any_fc(REG, 1) is True
    assert c.calls == ["fc06"]
    assert REG not in c._fc10_only_registers


def test_a_register_refused_on_both_codes_is_not_memoised():
    """Only a SUCCESSFUL FC 0x10 is evidence. Memoising a double refusal would silently
    stop trying the code that might start working."""
    c = _Client(single_ok=False, multi_ok=False)
    assert c.write_single_register_any_fc(REG, 1) is False
    assert REG not in c._fc10_only_registers
    c.calls.clear()
    assert c.write_single_register_any_fc(REG, 1) is False
    assert c.calls == ["fc06", "fc10"]
