"""Device identification must be a one-shot, off the poll path.

``_read_device_identification`` reads five holding blocks that a WIT does not answer
(serial 23-27, firmware 9-11, inverter type 125-132, protocol 30099, clock 30104/45).
It used to be gated on ``not self._serial_number`` — a "did it work?" flag, not a
"have we tried?" flag — so on this inverter it re-ran on *every* poll, forever.

While the socket was already broken each read failed instantly ([Errno 32] Broken pipe)
and the polls took ~3.5s, which hid the bug.  After a restart the socket was healthy,
every unanswered read cost a full pymodbus timeout x retries, and the poll took 205s
(2026-09-02 10:29:48.355 and 10:33:13.523, "Finished fetching ... in 205.x seconds").
All of it ran while the shared bus lock was held, so every ``set_wit_mode`` write in
that window failed with "Shared connection busy (lock timeout on write)".

These tests pin the properties that make that impossible:
  * one pass, ever — a second poll performs no identification reads at all,
  * an unanswered read is cached per register and never repeated,
  * a bus-busy read is *not* cached (it says nothing about the register),
  * identification reads go through the shared hub, not the private client,
  * a poll after identification is finished costs zero identification reads.
"""
import threading

import pytest


WIT_MAP = "WIT_4000_15000TL3"

# The five reads a WIT identification pass issues, in order.
WIT_IDENT_READS = [(23, 5), (9, 3), (125, 8), (30099, 1), (45, 6)]


class FakeHub:
    """Stand-in for SharedModbusConnection with a real lock and scripted answers."""

    def __init__(self, answers=None, busy=False):
        self._lock = threading.Lock()
        self.answers = dict(answers or {})
        self.reads = []
        self.connect_calls = 0
        self.busy = busy
        self.limits_entered = 0
        self.connection_generation = 0

    def ensure_connected(self):
        self.connect_calls += 1
        return True

    def optional_read_limits(self):
        hub = self

        class _Ctx:
            def __enter__(self):
                hub.limits_entered += 1

            def __exit__(self, *exc):
                return False

        return _Ctx()

    def read_holding_registers(self, start, count, slave_id):
        self.reads.append((start, count))
        return self.answers.get(start)

    # Test helper: hold the lock so the coordinator's acquire() times out.
    def hold_lock(self):
        self._lock.acquire()


class FakeGrowattModbus:
    """Only the attributes the identification path touches."""

    def __init__(self):
        self.client = None


def make_coordinator(coordinator_module, hub, register_map=WIT_MAP):
    """A coordinator with only the state the identification path needs."""
    coord = object.__new__(coordinator_module.GrowattModbusCoordinator)
    coord._hub = hub
    coord._client = FakeGrowattModbus()
    coord._slave_id = 1
    coord._register_map_key = register_map
    coord._serial_number = None
    coord._firmware_version = None
    coord._inverter_type = None
    coord._model_name = None
    coord._protocol_version = None
    coord._pending_clock_notification = None
    coord._identification_complete = False
    coord._identification_attempts = 0
    coord._identification_next_attempt = 0.0
    coord._identification_failed_reads = set()
    coord._identification_pending = False
    coord._identification_deferred = False
    return coord


# ---------------------------------------------------------------------------
# One pass, ever
# ---------------------------------------------------------------------------


def test_identification_runs_once_when_nothing_answers(coordinator_module):
    """The 205s poll: five unanswered reads, repeated on every poll. Never again."""
    hub = FakeHub(answers={})  # every read returns None
    coord = make_coordinator(coordinator_module, hub)

    coord._run_identification()
    assert hub.reads == WIT_IDENT_READS
    assert coord._identification_complete is True

    # Three more polls' worth of dispatch attempts: not one extra Modbus read.
    for _ in range(3):
        assert coord._identification_due() is False
        coord._run_identification()
    assert hub.reads == WIT_IDENT_READS
    assert coord._identification_attempts == 1


def test_identification_runs_once_when_everything_answers(coordinator_module):
    hub = FakeHub(answers={
        23: [0x4142, 0x4344, 0x4546, 0x4748, 0x494A],
        9: [0x0102, 0x0304, 0x0506],
        125: [0x5749, 0x5400, 0, 0, 0, 0, 0, 0],
        30099: [202],
        30104: [26, 9, 2, 10, 30, 0],
    })
    coord = make_coordinator(coordinator_module, hub)

    coord._run_identification()
    assert coord._serial_number == "ABCDEFGHIJ"
    assert coord._protocol_version == "Protocol 2.02"
    # A protocol version was detected, so the clock check uses the VPP registers.
    assert (30104, 6) in hub.reads
    assert coord._identification_complete is True

    reads_after_first_pass = list(hub.reads)
    coord._run_identification()
    assert hub.reads == reads_after_first_pass


def test_unanswered_reads_are_cached_per_register(coordinator_module):
    """Even a forced second pass re-reads only the registers that did answer."""
    hub = FakeHub(answers={9: [0x0102, 0x0304, 0x0506]})
    coord = make_coordinator(coordinator_module, hub)

    coord._run_identification()
    assert coord._identification_failed_reads == {
        "serial", "inverter_type", "protocol_version", "clock"
    }

    # Force another pass the way a retry window expiring would.
    coord._identification_complete = False
    coord._identification_next_attempt = 0.0
    hub.reads.clear()
    coord._run_identification()
    assert hub.reads == [(9, 3)]


def test_attempts_are_bounded(coordinator_module):
    """Even if every pass defers, identification stops after the attempt budget."""
    hub = FakeHub(answers={})
    coord = make_coordinator(coordinator_module, hub)

    for _ in range(10):
        coord._identification_complete = False   # simulate "always deferred"
        coord._identification_next_attempt = 0.0
        coord._run_identification()

    assert coord._identification_attempts == coordinator_module._IDENTIFICATION_MAX_ATTEMPTS
    assert coord._identification_due() is False


# ---------------------------------------------------------------------------
# A busy bus is not evidence about the register
# ---------------------------------------------------------------------------


def test_bus_busy_does_not_cache_a_failure(coordinator_module, monkeypatch):
    """Lock contention must not be mistaken for 'the inverter ignores this register'."""
    monkeypatch.setattr(coordinator_module, "SHARED_LOCK_TIMEOUT", 0.01)
    hub = FakeHub(answers={})
    hub.hold_lock()
    coord = make_coordinator(coordinator_module, hub)

    coord._run_identification()

    assert hub.reads == []                       # nothing reached the bus
    assert coord._identification_failed_reads == set()
    assert coord._identification_deferred is True
    assert coord._identification_complete is False   # a later pass may still succeed


# ---------------------------------------------------------------------------
# The right socket, and a bounded cost
# ---------------------------------------------------------------------------


def test_identification_uses_the_hub_not_the_private_client(coordinator_module):
    """``GrowattModbus.client`` is never connected in shared mode.

    Reading through it opened a second TCP session that the RS485 gateway does not
    service: at 10:29:52 the hub read holding 30100/30200/30407 in under a second while
    the private client could not read holding 30099 in 40s.
    """
    hub = FakeHub(answers={})
    coord = make_coordinator(coordinator_module, hub)

    class ExplodingClient:
        def read_holding_registers(self, *args, **kwargs):
            raise AssertionError("identification must not use the private client")

    coord._client.client = ExplodingClient()
    coord._run_identification()

    assert hub.reads == WIT_IDENT_READS
    assert hub.connect_calls == len(WIT_IDENT_READS)


def test_identification_reads_use_the_short_optional_timeout(coordinator_module):
    hub = FakeHub(answers={})
    coord = make_coordinator(coordinator_module, hub)
    coord._run_identification()
    assert hub.limits_entered == len(WIT_IDENT_READS)


def test_lock_is_released_between_identification_reads(coordinator_module):
    """A queued set_wit_mode write must never wait for the whole pass."""
    hub = FakeHub(answers={})
    coord = make_coordinator(coordinator_module, hub)
    held = []

    original = hub.read_holding_registers

    def spy(start, count, slave_id):
        held.append(hub._lock.locked())
        return original(start, count, slave_id)

    hub.read_holding_registers = spy
    coord._run_identification()

    assert all(held), "reads must happen under the lock"
    assert hub._lock.locked() is False, "the lock must be released after the pass"


def test_a_later_poll_costs_no_identification_reads(coordinator_module):
    """The poll-path gate: once identification is done, a poll never queues it again."""
    hub = FakeHub(answers={})
    coord = make_coordinator(coordinator_module, hub)
    coord._run_identification()
    hub.reads.clear()

    for _ in range(5):
        # This is exactly what _fetch_data_shared does at the end of a poll.
        if coord._identification_due():
            coord._identification_pending = True
    assert coord._identification_pending is False
    assert hub.reads == []


def test_optional_read_limits_shortens_then_restores(growatt_modbus):
    """The context manager must not leak its short timeout into critical reads."""

    class Params:
        timeout_connect = 10.0

    class Client:
        def __init__(self):
            self.comm_params = Params()
            self.timeout = 10.0
            self.retries = 3

    client = Client()
    with growatt_modbus.optional_read_limits(client):
        assert client.timeout == growatt_modbus.OPTIONAL_READ_TIMEOUT_SECONDS
        assert client.comm_params.timeout_connect == growatt_modbus.OPTIONAL_READ_TIMEOUT_SECONDS
        assert client.retries == growatt_modbus.OPTIONAL_READ_RETRIES

    assert client.timeout == 10.0
    assert client.comm_params.timeout_connect == 10.0
    assert client.retries == 3


def test_optional_read_limits_restores_after_an_exception(growatt_modbus):
    class Client:
        timeout = 10.0
        retries = 3

    client = Client()
    with pytest.raises(RuntimeError):
        with growatt_modbus.optional_read_limits(client):
            raise RuntimeError("boom")
    assert client.timeout == 10.0
    assert client.retries == 3
