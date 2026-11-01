# Upstream sync v2.0.6 - open questions

Written during the v2.0.6 merge (branch `sync/upstream-v2.0.6`, 2026-11-01). None of these
blocks the push: each one leaves behaviour as it was on the pre-merge fork `main`, and
none touches the battery optimizer's surfaces.

## 1. Device identification: keep the fork's one-shot, or adopt upstream's #438 rule?

Upstream fixed the same "identification repeats every poll" bug independently (#438,
#426): identification runs inline in the poll and completes once *anything* was learned,
so an inverter that answered nothing (asleep at startup) is asked again on the next poll.

The fork's version (f0d23f4) runs it off the poll path, takes the hub lock per read,
uses a 3 s / no-retry timeout and caches each unanswered read for the session, and closes
the pass after one undeferred attempt even if nothing answered. That last point means a
PV-only inverter that is asleep at HA startup would keep empty device info until the next
restart. The user's WIT hybrid never sleeps, so this has not mattered.

The merge keeps the fork's mechanism, routes it through upstream's `_read_holding` (so the
call sites are upstream's verbatim), and adds upstream's `any(...)` completion flag, which
the fork's `_run_identification` then overrides. Question: drop the override so an
inverter that answered nothing is retried (every 300 s, short timeouts), or keep it?
Proposed: drop it in a follow-up - low risk, but it is a behaviour change, so not in a
sync.

## 2. Two WIT entity-id changes on upgrade (decided upstream, FYI)

- `sensor.growatt_dry_contact_state` is removed (upstream v2.0.6: MIN TL-X only).
- `sensor.growatt_battery_battery_state_of_health` becomes
  `sensor.growatt_battery_battery_state_of_health_vpp` (upstream v2.0.0 renamed the VPP
  SOH sensor; the entity-id migration follows the display name).

Neither is an optimizer entity. Both are documented in RELEASENOTES.md and pinned in
`tests/test_wit_entity_keys.py`. Check whether any dashboard or automation in the HA
config uses them before the real deploy.

## 3. set_wit_mode now raises HomeAssistantError instead of ValueError (FYI)

Upstream #461 converted every service error in diagnostic.py; the fork's set_wit_mode was
the last holdout and `tests/test_xh2_validated.py` requires none remain. The optimizer
treats any raised call as a confirmed failure, so nothing changes for it; the log message
it records is now the real reason rather than "Unknown error".
