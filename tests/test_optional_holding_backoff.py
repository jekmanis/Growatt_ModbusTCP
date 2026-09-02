"""Optional VPP holding-block backoff and the derived WIT mode status.

Registers 30100 (control authority), 30200-30201 (export limit) and 30407-30410
(remote power control) are read best-effort inside ``_read_device_info``: a failure
is tolerated because some firmware variants don't implement them.

The failure handling used to be a permanent, session-lifetime blacklist keyed on a
single failed read.  ``read_holding_registers`` turns *any* exception into ``None``,
so one transient Modbus error on a healthy inverter silenced all three blocks for the
life of the process.  ``GrowattData`` is rebuilt per poll, so from then on the
dataclass defaults (``remote_power_control_enable = 0``) were published as if they had
been read, and ``sensor.growatt_inverter_mode`` reported "Passthrough" for 30 h while
the inverter was executing a discharge override.

These tests pin the three properties that make that impossible:
  * a block is skipped only after several *consecutive* failures,
  * the skip expires and the block is retried,
  * a block that was not read never reaches the derived mode status or the controls.
"""
import time

import pytest


WIT_MAP = "WIT_4000_15000TL3"


class FakeHoldingReads:
    """Stand-in for ``GrowattModbus.read_holding_registers``.

    ``fail`` holds start addresses that return ``None`` (what the real method does
    for every error, including exceptions).  ``values`` supplies register contents
    for specific start addresses; anything else reads back as zeros.
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

    def starts(self):
        return [start for start, _ in self.calls]


class FakeHub:
    """Minimal stand-in for SharedModbusConnection (generation counter only)."""

    def __init__(self):
        self.connection_generation = 0

    def reconnect(self):
        self.connection_generation += 1


@pytest.fixture
def wit_client(growatt_modbus):
    """A WIT GrowattModbus instance whose holding reads are fully controlled."""
    client = growatt_modbus.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502, register_map=WIT_MAP
    )
    return client


def _read_device_info(growatt_modbus, client, reads):
    """Run _read_device_info with the given fake reads, returning fresh GrowattData."""
    client.read_holding_registers = reads
    data = growatt_modbus.GrowattData()
    client._read_device_info(data)
    return data


# ---------------------------------------------------------------------------
# Threshold: a transient failure must not blacklist anything
# ---------------------------------------------------------------------------


def test_single_failure_does_not_skip_the_block(growatt_modbus, wit_client):
    """One failed read must leave the block eligible on the very next poll."""
    reads = FakeHoldingReads(fail={30100, 30200, 30407})
    _read_device_info(growatt_modbus, wit_client, reads)

    for anchor in (30100, 30200, 30407):
        assert wit_client._optional_holding_blocked(anchor) is False

    # Second poll must still attempt all three anchors.
    reads = FakeHoldingReads(fail={30100, 30200, 30407})
    _read_device_info(growatt_modbus, wit_client, reads)
    for anchor in (30100, 30200, 30407):
        assert anchor in reads.starts()


def test_block_skipped_only_after_threshold_consecutive_failures(growatt_modbus, wit_client):
    threshold = growatt_modbus._OPTIONAL_HOLDING_FAIL_THRESHOLD
    assert threshold >= 2, "a threshold of 1 is the bug this test guards against"

    for poll in range(1, threshold + 1):
        reads = FakeHoldingReads(fail={30407})
        _read_device_info(growatt_modbus, wit_client, reads)
        assert 30407 in reads.starts(), f"poll {poll} should still attempt the block"

    assert wit_client._optional_holding_blocked(30407) is True

    reads = FakeHoldingReads(fail={30407})
    _read_device_info(growatt_modbus, wit_client, reads)
    assert 30407 not in reads.starts()


def test_success_clears_the_failure_count(growatt_modbus, wit_client):
    """A good read resets the streak, so the threshold counts *consecutive* failures."""
    threshold = growatt_modbus._OPTIONAL_HOLDING_FAIL_THRESHOLD

    for _ in range(threshold - 1):
        _read_device_info(growatt_modbus, wit_client, FakeHoldingReads(fail={30407}))

    _read_device_info(
        growatt_modbus,
        wit_client,
        FakeHoldingReads(values={30407: [1, 20, 65436, 1]}),
    )
    assert 30407 not in wit_client._failed_optional_holding_addrs

    _read_device_info(growatt_modbus, wit_client, FakeHoldingReads(fail={30407}))
    assert wit_client._optional_holding_blocked(30407) is False


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_blacklist_expires_and_the_block_is_retried(growatt_modbus, wit_client):
    threshold = growatt_modbus._OPTIONAL_HOLDING_FAIL_THRESHOLD
    retry_seconds = growatt_modbus._OPTIONAL_HOLDING_RETRY_SECONDS

    for _ in range(threshold):
        _read_device_info(growatt_modbus, wit_client, FakeHoldingReads(fail={30407}))
    assert wit_client._optional_holding_blocked(30407) is True

    # Age the entry past the retry window.
    _, fail_count = wit_client._failed_optional_holding_addrs[30407]
    wit_client._failed_optional_holding_addrs[30407] = (
        time.time() - retry_seconds - 1,
        fail_count,
    )
    assert wit_client._optional_holding_blocked(30407) is False

    reads = FakeHoldingReads(values={30407: [1, 20, 65436, 1]})
    data = _read_device_info(growatt_modbus, wit_client, reads)
    assert 30407 in reads.starts()
    assert data.vpp_remote_power_available is True
    assert data.remote_power_control_enable == 1
    assert wit_client._failed_optional_holding_addrs == {}


def test_expired_entry_keeps_its_count_so_the_warning_is_not_repeated(growatt_modbus, wit_client):
    """Re-failing after expiry must increment, not restart, the streak."""
    threshold = growatt_modbus._OPTIONAL_HOLDING_FAIL_THRESHOLD
    retry_seconds = growatt_modbus._OPTIONAL_HOLDING_RETRY_SECONDS

    for _ in range(threshold):
        _read_device_info(growatt_modbus, wit_client, FakeHoldingReads(fail={30407}))
    wit_client._failed_optional_holding_addrs[30407] = (
        time.time() - retry_seconds - 1,
        threshold,
    )

    _read_device_info(growatt_modbus, wit_client, FakeHoldingReads(fail={30407}))
    assert wit_client._failed_optional_holding_addrs[30407][1] == threshold + 1
    assert wit_client._optional_holding_blocked(30407) is True


# ---------------------------------------------------------------------------
# Clear on (re-)connect
# ---------------------------------------------------------------------------


def test_reconnect_clears_both_blacklists(growatt_modbus):
    hub = FakeHub()
    client = growatt_modbus.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502,
        register_map=WIT_MAP, shared_conn=hub,
    )
    now = time.time()
    client._failed_optional_ranges = {(31200, 24): (now, 4)}
    client._failed_optional_holding_addrs = {30407: (now, 4), 30200: (now, 4)}

    # Same connection: state survives.
    client._sync_optional_blacklists_with_connection()
    assert client._failed_optional_holding_addrs != {}

    hub.reconnect()
    client._sync_optional_blacklists_with_connection()
    assert client._failed_optional_ranges == {}
    assert client._failed_optional_holding_addrs == {}


def test_read_all_data_clears_blacklists_after_a_reconnect(growatt_modbus):
    """The clear must be wired into the poll, not only available as a helper."""
    hub = FakeHub()
    client = growatt_modbus.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502,
        register_map=WIT_MAP, shared_conn=hub,
    )
    # Empty profile makes read_all_data bail immediately after the sync step.
    client.register_map = {"name": "STUB", "input_registers": {}, "holding_registers": {}}
    client._failed_optional_holding_addrs = {30407: (time.time(), 9)}

    hub.reconnect()
    assert client.read_all_data() is None
    assert client._failed_optional_holding_addrs == {}


class _FakeTcpClient:
    """Just enough of a pymodbus TCP client for the hub's connect/close cycle.

    pymodbus is a real test dependency now (the suite installs it), so an unpatched
    hub would genuinely dial 10.0.0.1:502 and block on the connect timeout. Patching
    the module-level name is upstream's own idiom - see
    tests/test_serial_shared_connection.py, which does the same for ``ModbusClient``.
    """

    def __init__(self, *args, **kwargs):
        self.socket = None

    def connect(self):
        self.socket = object()
        return True

    def is_socket_open(self):
        return self.socket is not None

    def close(self):
        # reset() only calls disconnect(), which does not drop the client object, so
        # is_socket_open() going False here is what makes the next ensure_connected()
        # open a genuinely new session and bump the generation.
        self.socket = None


@pytest.fixture
def fake_tcp_client(growatt_modbus, monkeypatch):
    """Make ``SharedModbusConnection.ensure_connected()`` cheap and offline.

    Every test below that connects a hub needs this. Without it the hub really dials
    10.0.0.1:502 and each connect burns the full 10 s pymodbus timeout.
    """
    monkeypatch.setattr(growatt_modbus, "ModbusTcpClient", _FakeTcpClient)
    return _FakeTcpClient


def test_shared_connection_bumps_generation_on_each_fresh_connect(growatt_modbus, fake_tcp_client):
    hub = growatt_modbus.SharedModbusConnection("10.0.0.1", 502)
    assert hub.connection_generation == 0

    assert hub.ensure_connected() is True
    assert hub.connection_generation == 1

    # Socket already open — no new session, no bump.
    assert hub.ensure_connected() is True
    assert hub.connection_generation == 1

    hub.reset("test")
    assert hub.ensure_connected() is True
    assert hub.connection_generation == 2


# ---------------------------------------------------------------------------
# A skipped block must not feed defaults into the derived mode status
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Control entities must not publish defaults either
# ---------------------------------------------------------------------------


def test_availability_map_covers_every_vpp_block_control(component_const, growatt_modbus):
    flags = component_const.VPP_CONTROL_AVAILABILITY_FLAG
    data_fields = growatt_modbus.GrowattData().__dict__

    for control_name, flag in flags.items():
        assert control_name in component_const.WRITABLE_REGISTERS, control_name
        assert flag in data_fields, flag
        assert control_name in data_fields, control_name

    # Every control backed by one of the three optional blocks must be listed,
    # otherwise its entity would go on publishing the dataclass default.
    anchors = {30100: 'vpp_control_authority_available',
               30200: 'vpp_export_limit_available',
               30407: 'vpp_remote_power_available'}
    expected = {
        'control_authority': anchors[30100],
        'vpp_export_limit_enable': anchors[30200],
        'vpp_export_limit_power_rate': anchors[30200],
        'remote_power_control_enable': anchors[30407],
        'remote_power_control_charging_time': anchors[30407],
        'remote_charge_and_discharge_power': anchors[30407],
    }
    assert flags == expected


def test_control_entities_consult_the_availability_map():
    """select.py / number.py can't be imported without the full HA runtime, so the
    wiring is checked at source level (same approach as test_sensor_integrity.py)."""
    from pathlib import Path

    component_dir = Path(__file__).parent.parent / "custom_components" / "growatt_modbus"
    for filename in ("select.py", "number.py"):
        src = (component_dir / filename).read_text(encoding="utf-8")
        assert "VPP_CONTROL_AVAILABILITY_FLAG" in src, filename
        # Both the availability property and the value property must gate on it.
        assert src.count("VPP_CONTROL_AVAILABILITY_FLAG.get(self._control_name)") == 2, filename
        assert "def available(self) -> bool:" in src, filename


# ---------------------------------------------------------------------------
# Why a read failed decides whether a reconnect re-arms it
# ---------------------------------------------------------------------------


class KindedHoldingReads(FakeHoldingReads):
    """FakeHoldingReads that also reports *why* a read failed, like the real method."""

    def __init__(self, fail=(), values=None, kind=None, client=None):
        super().__init__(fail=fail, values=values)
        self.kind = kind
        self.client = client

    def __call__(self, start_address, count):
        result = super().__call__(start_address, count)
        if self.client is not None:
            self.client._last_read_error_kind = self.kind if result is None else None
        return result


def _fail_block_until_blocked(growatt_modbus, client, anchor, kind):
    """Fail ``anchor`` enough consecutive polls that the backoff engages."""
    for _ in range(growatt_modbus._OPTIONAL_HOLDING_FAIL_THRESHOLD):
        reads = KindedHoldingReads(fail={anchor}, kind=kind, client=client)
        _read_device_info(growatt_modbus, client, reads)
    assert client._optional_holding_blocked(anchor) is True


def _shared_client(growatt_modbus, hub):
    return growatt_modbus.GrowattModbus(
        connection_type="tcp", host="10.0.0.1", port=502,
        register_map=WIT_MAP, shared_conn=hub,
    )


def test_no_response_backoff_survives_a_reconnect(growatt_modbus):
    """A register the inverter ignores must stay backed off across reconnects.

    This is the loop that made a poll cost a full timeout per unanswered register over
    and over: a timed-out read closes the pymodbus socket ("...CLOSING CONNECTION"), the
    next poll reconnects, the reconnect cleared the backoff, and the expensive read was
    armed again for the very next poll.
    """
    hub = FakeHub()
    client = _shared_client(growatt_modbus, hub)
    _fail_block_until_blocked(
        growatt_modbus, client, 30407, growatt_modbus.ERROR_KIND_NO_RESPONSE
    )

    hub.reconnect()
    client._sync_optional_blacklists_with_connection()

    assert client._optional_holding_blocked(30407) is True
    # And the very next poll issues no read for it.
    reads = KindedHoldingReads(fail={30407}, kind=growatt_modbus.ERROR_KIND_NO_RESPONSE,
                               client=client)
    _read_device_info(growatt_modbus, client, reads)
    assert 30407 not in reads.starts()


def test_link_failure_backoff_is_cleared_by_a_reconnect(growatt_modbus):
    """The earlier fix's intent: a dead socket must not silence the VPP blocks."""
    hub = FakeHub()
    client = _shared_client(growatt_modbus, hub)
    _fail_block_until_blocked(growatt_modbus, client, 30407, growatt_modbus.ERROR_KIND_LINK)

    hub.reconnect()
    client._sync_optional_blacklists_with_connection()

    assert client._optional_holding_blocked(30407) is False
    reads = KindedHoldingReads(values={30407: [1, 20, 65436, 1]}, client=client)
    data = _read_device_info(growatt_modbus, client, reads)
    assert 30407 in reads.starts()
    assert data.vpp_remote_power_available is True
    assert data.remote_power_control_enable == 1


def test_unclassified_failure_is_treated_as_a_link_failure(growatt_modbus):
    """Unknown provenance stays conservative — a transient error never sticks."""
    hub = FakeHub()
    client = _shared_client(growatt_modbus, hub)
    _fail_block_until_blocked(growatt_modbus, client, 30407, None)

    hub.reconnect()
    client._sync_optional_blacklists_with_connection()
    assert client._optional_holding_blocked(30407) is False


def test_a_poll_with_every_optional_block_backed_off_reads_none_of_them(growatt_modbus):
    """The whole point: a poll must not pay for reads that are known not to answer."""
    hub = FakeHub()
    client = _shared_client(growatt_modbus, hub)
    for anchor in (30100, 30200, 30407):
        _fail_block_until_blocked(
            growatt_modbus, client, anchor, growatt_modbus.ERROR_KIND_NO_RESPONSE
        )

    hub.reconnect()
    client._sync_optional_blacklists_with_connection()

    reads = KindedHoldingReads(fail={30100, 30200, 30407},
                               kind=growatt_modbus.ERROR_KIND_NO_RESPONSE, client=client)
    data = _read_device_info(growatt_modbus, client, reads)

    assert not ({30100, 30200, 30407} & set(reads.starts()))
    # The poll still completed, and the controls correctly report "not read".
    assert data.vpp_remote_power_available is False
    assert data.vpp_export_limit_available is False
    assert data.vpp_control_authority_available is False


# ---------------------------------------------------------------------------
# The hub classifies the two failure modes
# ---------------------------------------------------------------------------


class _ErrorResponse:
    def isError(self):
        return True


class _GoodResponse:
    registers = [7]

    def isError(self):
        return False


def test_hub_reports_no_response_for_an_error_response(growatt_modbus, fake_tcp_client):
    hub = growatt_modbus.SharedModbusConnection("10.0.0.1", 502)
    hub.ensure_connected()
    hub._client.read_holding_registers = lambda **kw: _ErrorResponse()

    assert hub.read_holding_registers(30099, 1, 1) is None
    assert hub.last_error_kind == growatt_modbus.ERROR_KIND_NO_RESPONSE


def test_hub_reports_link_for_a_transport_exception(growatt_modbus, fake_tcp_client):
    hub = growatt_modbus.SharedModbusConnection("10.0.0.1", 502)
    hub.ensure_connected()

    def boom(**kw):
        raise OSError(32, "Broken pipe")

    hub._client.read_holding_registers = boom

    assert hub.read_holding_registers(30099, 1, 1) is None
    assert hub.last_error_kind == growatt_modbus.ERROR_KIND_LINK


def test_hub_clears_the_error_kind_on_success(growatt_modbus, fake_tcp_client):
    hub = growatt_modbus.SharedModbusConnection("10.0.0.1", 502)
    hub.ensure_connected()
    hub.last_error_kind = growatt_modbus.ERROR_KIND_LINK
    hub._client.read_holding_registers = lambda **kw: _GoodResponse()

    assert hub.read_holding_registers(30099, 1, 1) == [7]
    assert hub.last_error_kind is None
