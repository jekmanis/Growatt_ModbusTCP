"""A skipped VPP holding block must not feed defaults into the fork's derived WIT mode.

Fork-only. These tests lived in tests/test_optional_holding_backoff.py until upstream
adopted the backoff mechanism itself (v2.0.x, #370) and shipped its own file of that name;
the backoff tests are now upstream's, and the part upstream has no counterpart for - the
fork's `_compute_wit_mode_status` - is pinned here.

Registers 30100 (control authority), 30200-30201 (export limit) and 30407-30410 (remote
power control) are read best-effort. `GrowattData` is rebuilt per poll, so a block that
was not read carries dataclass defaults (`remote_power_control_enable = 0`). Published as
if read, those made `sensor.growatt_inverter_mode` report "Passthrough" for 30 h while the
inverter was executing a discharge override. The battery optimizer verifies every
set_wit_mode against that sensor, so a fabricated mode makes it resend or give up.
"""
import pytest


WIT_MAP = "WIT_4000_15000TL3"


class FakeHoldingReads:
    """Stand-in for ``GrowattModbus.read_holding_registers``.

    ``fail`` holds start addresses that return ``None`` (what the real method does for
    every error, including exceptions). ``values`` supplies register contents for
    specific start addresses; anything else reads back as zeros.
    """

    def __init__(self, fail=(), values=None):
        self.fail = set(fail)
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, start_address, count):
        self.calls.append((start_address, count))
        if start_address in self.fail:
            return None
        if start_address in self.values:
            return list(self.values[start_address])
        return [0] * count


@pytest.fixture
def wit_client(growatt_modbus):
    """A WIT GrowattModbus instance whose holding reads are fully controlled."""
    return growatt_modbus.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502, register_map=WIT_MAP
    )


def _read_device_info(growatt_modbus, client, reads):
    """Run _read_device_info with the given fake reads, returning fresh GrowattData."""
    client.read_holding_registers = reads
    data = growatt_modbus.GrowattData()
    client._read_device_info(data)
    return data


def _mode(coordinator_module, growatt_modbus, **fields):
    data = growatt_modbus.GrowattData(**fields)
    coordinator_module.GrowattModbusCoordinator._compute_wit_mode_status(None, data)
    return data


def test_mode_status_unknown_when_remote_power_block_was_skipped(
    coordinator_module, growatt_modbus
):
    """The exact production regression: defaults must not read as Passthrough."""
    data = _mode(coordinator_module, growatt_modbus)  # every *_available flag False
    assert data.wit_mode_status == "Unknown"
    assert data.wit_mode_override_active is False
    assert data.wit_mode_export_rate == -1


def test_mode_status_passthrough_only_when_the_block_was_read(
    coordinator_module, growatt_modbus
):
    data = _mode(
        coordinator_module, growatt_modbus,
        vpp_remote_power_available=True,
        vpp_export_limit_available=True,
        remote_power_control_enable=0,
    )
    assert data.wit_mode_status == "Passthrough"


def test_live_discharge_to_load_is_reported_when_blocks_respond(
    coordinator_module, growatt_modbus
):
    """Register values observed on the inverter during the stuck-Passthrough window."""
    data = _mode(
        coordinator_module, growatt_modbus,
        vpp_remote_power_available=True,
        vpp_export_limit_available=True,
        remote_power_control_enable=1,          # 30407
        remote_power_control_charging_time=20,  # 30408
        remote_charge_and_discharge_power=-100,  # 30409
        vpp_export_limit_enable=1,              # 30200
        vpp_export_limit_power_rate=0,          # 30201
    )
    assert data.wit_mode_status == "Discharge to Load"
    assert data.wit_mode_power_percent == 100
    assert data.wit_mode_override_active is True


def test_discharge_is_unknown_when_the_export_limit_block_was_skipped(
    coordinator_module, growatt_modbus
):
    """Without 30200-30201 there is no way to tell "to grid" from "to load"."""
    data = _mode(
        coordinator_module, growatt_modbus,
        vpp_remote_power_available=True,
        vpp_export_limit_available=False,
        remote_power_control_enable=1,
        remote_charge_and_discharge_power=-100,
    )
    assert data.wit_mode_status == "Unknown"
    assert data.wit_mode_export_rate == -1


def test_grid_charge_does_not_depend_on_the_export_block(
    coordinator_module, growatt_modbus
):
    data = _mode(
        coordinator_module, growatt_modbus,
        vpp_remote_power_available=True,
        vpp_export_limit_available=False,
        remote_power_control_enable=1,
        remote_charge_and_discharge_power=50,
    )
    assert data.wit_mode_status == "Grid Charge"
    assert data.wit_mode_power_percent == 50


def test_end_to_end_skipped_block_yields_unknown_mode(
    growatt_modbus, coordinator_module, wit_client
):
    """Poll with the VPP blocks failing -> data defaults -> mode status Unknown."""
    data = _read_device_info(
        growatt_modbus, wit_client, FakeHoldingReads(fail={30100, 30200, 30407})
    )
    assert data.vpp_remote_power_available is False
    assert data.remote_power_control_enable == 0  # dataclass default, never read

    coordinator_module.GrowattModbusCoordinator._compute_wit_mode_status(None, data)
    assert data.wit_mode_status == "Unknown"
