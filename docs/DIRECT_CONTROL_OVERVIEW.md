# Direct Control Architecture: Solution Overview

## Problem Statement

The current battery optimizer uses **TOU (Time-of-Use) schedules** written to inverter registers 30411-30471 to control the WIT 8000TL3-HU inverter. This approach has fundamental limitations:

| Limitation | Impact |
|---|---|
| 20-period hardware cap | Cannot express 24h schedule at 15-min granularity (need 96 slots) |
| TOU only controls charge/discharge | **Cannot control grid export** per time slot |
| No dynamic SOC limits | Same SOC floor/ceiling applies to all periods |
| Complex write ceremony | Clear 60 registers + write + verify = 10-40 seconds per sync |
| Rolling boundary workaround | Extra complexity to handle day boundaries within 20 periods |
| TOU doesn't trigger grid charging | V1.39 firmware ignores TOU periods for AC charging — only remote control (30407) works |
| Bus contention | HA coordinator polling blocks optimizer register writes |

## Solution: Direct Command Control

Replace TOU schedule writes with **real-time mode commands** sent every optimizer slot (15 min). The WIT's VPP protocol supports time-limited overrides via registers 30407-30409 with a configurable duration (30408). This is exactly what a direct controller needs.

### Key Insight

The WIT's "time-limited override" model, which was a limitation for manual control, becomes a **safety feature** for automated control:

```
Optimizer sends: "Charge at 80% for 20 minutes"
  |
If optimizer sends next command in 15 min -> override refreshed, continuous control
If optimizer crashes -> override expires in 20 min -> inverter reverts to safe base mode
```

## Architecture

```
+------------------------------------------------------------------+
|                    BATTERY OPTIMIZER (AppDaemon)                  |
|                                                                  |
|  Nord Pool Prices --> DP Optimizer --> Schedule                   |
|  Load Forecast    -->              --> (mode per 15-min slot)     |
|  Battery SOC      -->                                            |
|  PV Production    -->                                            |
|                                                                  |
|  Every 15 min:                                                   |
|    schedule[now] -> { mode, power%, export_rate, ac_charge, SOC }|
|         |                                                        |
|         v                                                        |
|    HA Service Call: growatt_modbus/set_wit_mode                   |
+------------+-----------------------------------------------------+
             |
             v
+------------------------------------------------------------------+
|              GROWATT MODBUS INTEGRATION (HA)                      |
|                                                                  |
|  set_wit_mode service:                                           |
|    1. Validate parameters                                        |
|    2. Atomic multi-register write:                               |
|       +- 30100 = 1        (VPP control authority)                |
|       +- 30476 = 0/1      (priority: 1=Battery First or 0=Load First) |
|       +- 30410 = 0/1      (AC charge mode)                      |
|       +- 30404 = soc%     (charge cutoff SOC)                   |
|       +- 30405 = soc%     (discharge cutoff SOC)                |
|       +- 30200 = 0/1      (export limit enable)                 |
|       +- 30201 = rate%    (export limit rate)                    |
|       +- 30411 = 0        (clear stale TOU periods)             |
|       +- 30408 = minutes  (override duration)                   |
|       +- 30409 = power%   (charge/discharge power)              |
|       +- 30407 = 0/1      (remote control enable -- LAST)       |
|    3. Update coordinator state                                   |
|    4. Return success/failure                                     |
|                                                                  |
|  Mode Status Sensor:                                             |
|    Reads 30407, 30409, 30200, 30201, 30410, 30411, 30476         |
|    Reports: "Grid Charge (80%, 18 min left, export off)"         |
|                                                                  |
|  Mode Select Entity (manual presets):                            |
|    Grid Charge / Discharge to Load / Discharge to Grid /         |
|    Max Export / Preserve SOC / Passthrough                       |
+------------------------------------------------------------------+
             |
             v
+------------------------------------------------------------------+
|                    WIT 8000TL3-HU INVERTER                       |
|                                                                  |
|  VPP Override Engine:                                            |
|    Active override -> follows commanded power/export             |
|    Override expired -> reverts to panel-configured base mode     |
|                                                                  |
|  Base mode (register 30476, READ-ONLY via Modbus):               |
|    Configured on inverter panel: Load First / Battery First      |
|    Acts as safety fallback when no override is active            |
+------------------------------------------------------------------+
```

## Control Axes

The optimizer controls the inverter via **five independent axes**, all settable in a single `set_wit_mode` call:

### Axis 1: Battery Power (registers 30407 + 30408 + 30409)

| Parameter | Register | Range | Meaning |
|---|---|---|---|
| Enable | 30407 | 0-1 | 0=release override, 1=activate |
| Duration | 30408 | 0-1440 min | How long override stays active |
| Power | 30409 | -100 to +100% | Positive=charge, negative=discharge |

**Sign encoding:** Positive values (1-100) are written directly for charging. Negative values for discharging are encoded as unsigned 16-bit: `65536 - abs(power)` (e.g., -100% = 65436).

### Axis 2: Export Rate (registers 30200 + 30201)

| Parameter | Register | Range | Meaning |
|---|---|---|---|
| Enable limiting | 30200 | 0-1 | 0=no limit (full export), 1=limit active |
| Rate | 30201 | -100 to +100% | Export power rate when limiting enabled |

Combinations:
- **Full export**: 30200=0 (limiter disabled)
- **Zero export**: 30200=1, 30201=0
- **Partial export** (e.g., 50%): 30200=1, 30201=50

**Important:** Stale zero-export (30200=1, 30201=0) from a previous mode can block grid charging. The service explicitly clears export limits for grid_charge and other modes that need export enabled.

### Axis 3: AC Charge Mode (register 30410)

| Value | Mode | Behavior |
|---|---|---|
| 0 | Disabled | No AC/grid charging -- solar only |
| 1 | PV priority | PV charges battery first; AC supplements to reach target power |
| 2 | AC priority | **Rejected by V1.39 firmware** (Illegal Function exception) |

**Firmware limitation:** WIT V1.39 only accepts 30410=0 (disabled) and 30410=1 (PV priority). AC priority (30410=2) returns Modbus exception code 1 on both FC 0x06 and FC 0x10. Grid charging works via remote control (30407=1, 30409=+power%) with PV priority set.

### Axis 4: SOC Limits (registers 30404 + 30405)

| Parameter | Register | Range | Meaning |
|---|---|---|---|
| Charge cutoff | 30404 | 10-100% | Stop charging at this SOC |
| Discharge cutoff | 30405 | 10-100% | Stop on-grid discharge at this SOC |

### Axis 5: Priority / Base Mode (register 30476)

| Value | Mode | Effect when 30407=0 | Effect when 30407=1 |
|---|---|---|---|
| 0 | Load First | PV powers load, surplus charges battery, rest exports. **Safe default.** | **Blocks grid charging!** PV surplus does NOT charge battery. |
| 1 | Battery First | Prioritize battery charging from all sources | **Required for grid charging.** PV surplus charges battery normally. |
| 2 | Grid First | **Discharges battery to grid at 6kW! Dangerous.** | Untested / unsafe. |

**Critical register — must be set explicitly for EVERY mode.** Register 30476 affects inverter behavior both with and without remote control (30407). Leaving it inherited from a previous mode causes failures:

- **30476=0 + 30407=1 + 30409=+100** → grid charging produces **0W** (broken!)
- **30476=1 + 30407=1 + 30409=+100** → grid charging produces **3kW** (working!)
- **30476=2 + 30407=0** → battery discharges to grid at 6kW (dangerous for hold)
- **30476=0 + 30407=1 + discharge** → PV surplus does NOT charge battery

The `set_wit_mode` service now sets 30476 explicitly for every mode:
- **30476=1** (Battery First) for all modes with 30407=1: grid_charge, discharge_to_load, discharge_to_grid, max_export
- **30476=0** (Load First) for all modes with 30407=0: hold, preserve_soc, passthrough

## Mode Definitions

These are the **preset modes** available in the HA select entity and as shorthand in the service:

| Mode | 30476 | 30407 | 30409 | 30200 | 30201 | 30410 | Description |
|---|---|---|---|---|---|---|---|
| `grid_charge` | **1** | 1 | +power% | 0 | -- | 1 | Charge from grid+PV. 30476=1 required! |
| `discharge_to_load` | **1** | 1 | -power% | 1 | 0 | 0 | Discharge to cover load. Zero export. |
| `discharge_to_grid` | **1** | 1 | -power% | 0 | -- | 0 | Discharge with export for selling. |
| `max_export` | **1** | 1 | -100 | 0 | -- | 0 | Full discharge + max export. |
| `hold` / `preserve_soc` | **0** | 0 | 0 | 0 | -- | 0 | Battery idle. PV covers load + exports. |
| `passthrough` | **0** | 0 | 0 | 0 | -- | 0 | Release all overrides. Defensive zero-out. |

**Every mode explicitly sets all critical registers.** No stale values remain on hardware between mode switches. "--" for 30201 means it is not written when 30200=0 (limiter disabled, rate irrelevant).

**Key findings from Modbus probing (2026-03-28/29/30):**
- `grid_charge`: TOU periods (30411-30414) alone do NOT trigger charging. Remote control (30407=1, 30409=+100) is required.
- `grid_charge`: **30476=1 (Battery First) is mandatory.** With 30476=0 or 30476=2, charging produces 0W even with 30407=1, 30409=+100. Only 30476=1 produces 3kW+ sustained charging. Confirmed 2026-03-30.
- `hold/preserve_soc`: 30407=1 with 30409=0 clips PV export to 0W. Setting 30407=0 lets PV export resume immediately.
- Stale 30200=1 (zero export) from previous `discharge_to_load` blocks grid charging. All modes now explicitly set 30200.
- **30476 (priority mode) affects behavior with AND without 30407=1.** When 30407=0, if 30476=2 (Grid First), battery discharges to grid at 6kW. When 30407=1, 30476=0 blocks grid charging and prevents PV surplus from charging battery.
- **Fix (v3): every mode now sets 30476 explicitly.** 30407=1 modes → 30476=1 (Battery First). 30407=0 modes → 30476=0 (Load First). No inheritance between modes.

## Register Write Order

The write order matters for the WIT VPP protocol:

```
1.  30100 = 1          (ensure VPP control authority is on)
2.  30476 = 0 or 1     (priority mode -- ALWAYS set, EVERY mode)
3.  30410 = ac_mode    (set AC charge mode BEFORE enabling remote control)
4.  30404 = soc%       (set charge cutoff BEFORE charging starts)
5.  30405 = soc%       (set discharge cutoff BEFORE discharging starts)
6.  30200 = 0/1        (export limit enable)
7.  30201 = rate%      (export limit rate)
8.  30411 = 0          (clear stale TOU periods -- always, for all modes)
9.  30408 = duration   (set duration BEFORE enabling)
10. 30409 = power%     (set power BEFORE enabling)
11. 30407 = 0 or 1     (enable/disable remote control -- ALWAYS LAST)
```

**Why 30476 is set for EVERY mode**: 30476 affects behavior both with and without remote control. Leaving it inherited from a previous mode causes grid charging to fail (30476=0 → 0W) or battery to discharge unexpectedly (30476=2 → 6kW discharge). Rule: 30407=1 modes get 30476=1, 30407=0 modes get 30476=0.

**Why 30407 is last**: Setting 30407=1 starts the override timer. All parameters must be configured before the timer starts.

**Why 30411=0 always**: TOU periods are no longer used for any mode. Clearing them prevents stale TOU state from interfering with remote control.

## Safety Model

### 1. Override Expiry (Hardware Safety)

```
Optimizer slot duration:  15 minutes (configurable)
Override duration:        slot_duration + buffer (e.g., 20 minutes)
Safety margin:            5 minutes

Timeline:
  T+0:00   Optimizer sends mode command (duration=20 min)
  T+15:00  Optimizer sends next command (refreshes override)
  T+15:00  Timer resets to 20 min

If optimizer crashes at T+5:00:
  T+20:00  Override expires -> inverter reverts to base mode (Load First)
  Impact:  Max 15 minutes of uncontrolled operation
```

### 2. SOC Boundaries (Software Safety)

The optimizer continues to enforce SOC boundaries in software:
- If SOC <= min_soc during DISCHARGE -> switch to HOLD
- If SOC >= max_soc during CHARGE -> switch to HOLD

Additionally, hardware SOC limits (30404/30405) provide a second safety layer:
- Even if optimizer fails to react, the inverter stops charging/discharging at the hardware cutoff

### 3. Rate Limiting (Integration Safety)

The integration enforces 30-second minimum intervals between writes to the same register. This prevents:
- Automation loops causing oscillation
- Rapid mode toggling stressing the inverter
- Modbus bus flooding

The `set_wit_mode` service bypasses this rate limiter for its coordinated multi-register writes, since it's a single atomic operation, not rapid toggling.

### 4. Mode Status Detection

The coordinator computes the current mode from register state:

| Register State | Detected Mode |
|---|---|
| 30407=1, 30409 > 1 | Grid Charge |
| 30407=1, 30409 in (0, 1) | Preserve SOC |
| 30407=1, 30409 < 0, export allowed, abs=100 | Max Export |
| 30407=1, 30409 < 0, export allowed | Discharge to Grid |
| 30407=1, 30409 < 0, zero export | Discharge to Load |
| 30407=0, coordinator tracking = hold | Preserve SOC |
| 30407=0, otherwise | Passthrough |

**Note:** Preserve SOC with 30407=0 has no unique register signature. Detection uses coordinator-tracked `wit_direct_mode` state, which resets on HA restart (shows as Passthrough until next command).

## Optimizer <-> Integration Data Flow

### Service Call Format

```yaml
service: growatt_modbus/set_wit_mode
data:
  device_id: "abc123def456"
  mode: "grid_charge"               # Required: charge/discharge/hold/passthrough/...
  power_percent: 80                  # Optional (default 100): 1-100
  duration_minutes: 20               # Optional (default 60): 1-1440
  export_rate: 0                     # Optional: 0-100 (0=zero export, 100=full export)
  ac_charge_mode: "pv_priority"      # Optional: disabled/pv_priority
  charge_cutoff_soc: 95              # Optional: 10-100
  discharge_cutoff_soc: 20           # Optional: 10-100
```

### Service Response

```yaml
success: true
mode_applied: "grid_charge"
registers_written:
  30100: 1
  30476: 0
  30410: 1
  30404: 95
  30200: 0
  30411: 0
  30408: 20
  30409: 80
  30407: 1
timestamp: "2026-03-29T11:00:00+03:00"
override_expires: "2026-03-29T11:20:00+03:00"
```

### Mode Status Sensor

```yaml
entity_id: sensor.growatt_wit_mode_status
state: "Grid Charge"
attributes:
  mode: "grid_charge"
  power_percent: 80
  duration_minutes: 20
  duration_remaining_minutes: 18
  export_rate: 100
  export_enabled: true
  ac_charge_mode: "pv_priority"
  charge_cutoff_soc: 95
  discharge_cutoff_soc: 20
  override_active: true
  override_expires: "2026-03-29T11:20:00+03:00"
  last_command_source: "service"
```

## What Changes, What Stays

### Growatt Modbus Integration (this repo)

| Change | Type | Description |
|---|---|---|
| `set_wit_mode` service | **NEW** | Composite mode-setting service with atomic multi-register writes |
| Mode Status sensor | **NEW** | Reports effective mode from control registers |
| Mode Select entity | **NEW** | Dropdown with 6 preset modes for manual/dashboard control |
| Existing number/select entities | **KEEP** | Granular control for advanced users and debugging |
| Existing switches (export, optimizer) | **KEEP** | Backward compatible; optimizer switch may be repurposed |
| `set_battery_mode` service | **KEEP** | Backward compatible; `set_wit_mode` is the recommended replacement |
| `sync_tou_schedule` service | **KEEP** | Available but not used by optimizer in direct mode |

### Battery Optimizer (separate repo)

| Change | Type | Description |
|---|---|---|
| `tou_sync.py` | **REPLACE** with `direct_control.py` | New module calling `set_wit_mode` service |
| `execute_scheduled_mode()` | **MODIFY** | Always calls `set_wit_mode` instead of conditionally skipping for TOU |
| `adaptive_optimize()` | **SIMPLIFY** | No rolling TOU boundary logic needed |
| `full_optimize()` | **SIMPLIFY** | No TOU sync step; just compute schedule + execute current slot |
| DP optimizer | **EXTEND** | Add export_rate as optimization variable; add ac_charge_mode logic |
| `models.py` | **EXTEND** | `ScheduleEntry` gains export_rate, ac_charge_mode, SOC limits |
| `config.py` | **MODIFY** | Remove TOU-specific config; add direct control config |
| Safety mechanisms | **KEEP** | SOC boundaries, PV override logic unchanged |
| Cost tracker | **KEEP** | Mode transition tracking unchanged |
| Load profiling | **KEEP** | Unaffected by control method change |

## Migration Path

1. **Phase 1**: Build `set_wit_mode` service + mode sensor in integration (no optimizer changes yet)
2. **Phase 2**: Test service manually from HA Developer Tools / automations
3. **Phase 3**: Build `direct_control.py` in optimizer, wire into `execute_scheduled_mode()`
4. **Phase 4**: Extend DP optimizer with export_rate decisions
5. **Phase 5**: Remove TOU sync code (or keep as dead code / optional fallback)

Each phase is independently testable and deployable.

---

*Document version: 3.0*
*Date: 2026-03-30*
*Updated: 30476 now set explicitly for EVERY mode. 30476=1 (Battery First) required for grid charging — 30476=0 produces 0W. All modes set all registers — no inherited values between mode switches.*
