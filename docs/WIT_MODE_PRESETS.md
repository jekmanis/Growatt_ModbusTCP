# WIT Mode Presets

## Grid Charge
Forces the battery to charge at full power from the grid (and PV if available). Uses remote power control (30407=1, 30409=+power%) with **30476=1 (Battery First)** — this combination was confirmed by Modbus probing to reliably trigger 3kW+ sustained charging on WIT V1.39 firmware. PV priority (30410=1) is always set — AC priority (30410=2) is rejected by this firmware. Override lasts 2 hours by default.

**Critical:** 30476=1 (Battery First) is **mandatory** for grid charging. With 30476=0 (Load First) or 30476=2 (Grid First), charging produces 0W even with all other registers correctly set. Discovered 2026-03-30 after systematic testing of all three values.

## Discharge to Load
Forces the battery to discharge at full power to cover your home's electricity consumption, but blocks all grid export (30200=1, 30201=0). Any power the battery produces beyond what your home needs is wasted rather than sold. Use this during expensive electricity hours when you want to avoid grid import but your feed-in tariff is zero or negative.

## Discharge to Grid
Forces the battery to discharge at full power with grid export enabled (30200=0). Power goes to your home load first, and any surplus is sold to the grid. Use this during high electricity price windows to sell stored energy for profit. Override lasts 2 hours by default.

## Max Export
Full-power discharge with unrestricted export — maximum selling. The battery discharges at 100% and all surplus beyond home load is exported. This is the most aggressive selling mode, used when the spot price is at its peak and you want to extract maximum revenue from stored energy.

## Preserve SOC
Battery sits idle — no charging from grid, no discharging. PV still powers your home first, and any surplus PV charges the battery. When the battery is full, surplus PV exports to the grid. Uses 30407=0 (remote control disabled) so the inverter doesn't clip PV export — confirmed by Modbus probing that 30407=1 with 30409=0 blocks PV export. Use this to preserve battery SOC for a planned discharge window later, or when electricity prices are mid-range and neither grid charging nor discharging makes economic sense.

## Passthrough
Releases all overrides completely. The inverter returns to whatever base mode is configured by register 30476 (reset to "Load First" by the service). No remote power control is active — the inverter follows its base mode. Use this to hand control back to the inverter's built-in logic, or as an emergency "undo everything" reset.

---

## Priority Mode (register 30476) — Set for EVERY Mode

Register 30476 controls the inverter's priority behavior and affects operation **both with and without** remote control (30407). It must be set explicitly for every mode — never inherited.

| 30476 value | Mode | Effect when 30407=0 | Effect when 30407=1 |
|---|---|---|---|
| 0 | Load First | PV covers load, surplus charges battery. **Safe.** | **Blocks grid charging (0W)!** PV surplus skips battery. |
| 1 | Battery First | Prioritize battery charging from all sources | **Required for grid charging.** PV surplus charges battery. |
| 2 | Grid First | **Discharges battery to grid at 6kW!** | Untested / unsafe |

### What each mode sets

| Mode | 30476 | Rationale |
|---|---|---|
| Grid Charge | **1** (Battery First) | **Mandatory** — 30476=0 and 30476=2 both produce 0W charging |
| Discharge to Load | **1** (Battery First) | PV surplus charges battery; 30476=0 would block this |
| Discharge to Grid | **1** (Battery First) | PV surplus charges battery during discharge |
| Max Export | **1** (Battery First) | Consistent with other 30407=1 modes |
| Preserve SOC | **0** (Load First) | Safe — prevents unintended battery discharge |
| Passthrough | **0** (Load First) | Safe — inverter follows normal Load First behavior |

### Discovery timeline (2026-03-30)
1. Stale 30476=2 (Grid First) caused "Preserve SOC" to discharge at 6kW → fix: set 30476=0 for hold modes
2. Setting 30476=0 for ALL modes broke "Discharge to Load" (PV surplus stopped charging battery) → fix: keep 30476 unchanged for 30407=1 modes
3. Grid charging failed with 30476=0 and 30476=2, only worked with **30476=1** → fix: set 30476=1 for all 30407=1 modes, 30476=0 for all 30407=0 modes. **No inheritance.**

---

## Using in Home Assistant

### Dashboard dropdown

The **Mode Preset** select entity (`select.growatt_<name>_mode_preset`) provides a dropdown with all six modes. Selecting a mode applies it immediately with sensible defaults (100% power, 2-hour duration for timed modes).

### Service call (full control)

Use the `growatt_modbus.set_wit_mode` service for fine-grained control over all parameters.

**Developer Tools > Services:**

```yaml
service: growatt_modbus.set_wit_mode
data:
  device_id: "<your_device_id>"
  mode: grid_charge
  power_percent: 80
  duration_minutes: 30
  charge_cutoff_soc: 95
```

Only `device_id` and `mode` are required. All other parameters are optional — omitted values use mode-specific defaults or leave the current setting unchanged.

### Automation examples

**Charge from grid during cheap night tariff:**

```yaml
automation:
  - alias: "Night charge on cheap tariff"
    trigger:
      - platform: time
        at: "01:00:00"
    action:
      - service: growatt_modbus.set_wit_mode
        data:
          device_id: "<your_device_id>"
          mode: grid_charge
          power_percent: 100
          duration_minutes: 360
          charge_cutoff_soc: 95
```

**Sell stored energy when spot price is high:**

```yaml
automation:
  - alias: "Sell when price above threshold"
    trigger:
      - platform: numeric_state
        entity_id: sensor.nordpool_electricity_price
        above: 0.15
    action:
      - service: growatt_modbus.set_wit_mode
        data:
          device_id: "<your_device_id>"
          mode: discharge_to_grid
          power_percent: 100
          duration_minutes: 60
          discharge_cutoff_soc: 20
```

**Hold battery before evening peak:**

```yaml
automation:
  - alias: "Preserve battery for evening peak"
    trigger:
      - platform: time
        at: "15:00:00"
    action:
      - service: growatt_modbus.set_wit_mode
        data:
          device_id: "<your_device_id>"
          mode: preserve_soc
```

**Return to normal when price normalizes:**

```yaml
automation:
  - alias: "Passthrough on normal price"
    trigger:
      - platform: numeric_state
        entity_id: sensor.nordpool_electricity_price
        below: 0.10
    action:
      - service: growatt_modbus.set_wit_mode
        data:
          device_id: "<your_device_id>"
          mode: passthrough
```

### Mode status sensor

The **Inverter Mode** sensor (`sensor.growatt_<name>_inverter_mode`) shows the current effective mode and exposes detailed attributes:

| Attribute | Description |
|---|---|
| `mode` | Current mode name |
| `power_percent` | Active power command (%) |
| `export_rate` | Current export rate (0-100%) |
| `ac_charge_mode` | disabled / pv_priority / ac_priority |
| `override_active` | Whether a timed override is running |
| `duration_remaining_minutes` | Minutes left on current override |
| `override_expires` | ISO timestamp when override ends |
| `charge_cutoff_soc` | Charge stop SOC (%) |
| `discharge_cutoff_soc` | Discharge stop SOC (%) |

Use these attributes in template conditions:

```yaml
condition:
  - condition: template
    value_template: "{{ state_attr('sensor.growatt_wit_inverter_mode', 'override_active') == false }}"
```

### Service call parameters reference

| Parameter | Required | Default | Range | Description |
|---|---|---|---|---|
| `device_id` | Yes | — | — | Growatt WIT device ID |
| `mode` | Yes | — | see presets | Operating mode |
| `power_percent` | No | 100 | 1-100 | Charge/discharge power level |
| `duration_minutes` | No | 60 | 1-1440 | Override duration before reverting to base mode |
| `export_rate` | No | mode default | 0-100 | Grid export rate (0=zero export, 100=full) |
| `ac_charge_mode` | No | mode default | disabled/pv_priority | Grid charging behavior (ac_priority rejected by V1.39 firmware) |
| `charge_cutoff_soc` | No | unchanged | 10-100 | Stop charging at this SOC |
| `discharge_cutoff_soc` | No | unchanged | 10-100 | Stop discharging at this SOC |
