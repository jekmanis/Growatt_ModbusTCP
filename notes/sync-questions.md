# Upstream sync v2.0.6 - open questions

None blocks the push: each keeps pre-merge fork behaviour and none touches the optimizer.

1. **Identification: fork one-shot vs upstream #438.** The merge keeps the fork's
   off-poll identification (hub lock per read, 3 s timeout, per-read failure cache) on top
   of upstream's `_read_holding` and `any(...)` completion flag. `_run_identification`
   still closes the pass after one undeferred attempt even if nothing answered, so a
   PV-only inverter asleep at HA startup keeps empty device info until restart (the WIT
   never sleeps). Proposed follow-up: drop that override so a silent inverter is retried
   every 300 s.
2. **Two WIT entity-id changes on upgrade, decided upstream.**
   `sensor.growatt_dry_contact_state` is removed (MIN TL-X only now);
   `sensor.growatt_battery_battery_state_of_health` becomes `..._state_of_health_vpp`.
   Not optimizer entities; documented in RELEASENOTES.md and pinned in
   test_wit_entity_keys.py. Check dashboards/automations before the real deploy.
3. **set_wit_mode raises HomeAssistantError, not ValueError** (upstream #461 rule, enforced
   by test_xh2_validated.py). The optimizer treats any raised call as a failure; only its
   logged message improves.
