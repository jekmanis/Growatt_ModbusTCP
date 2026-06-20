"""
SPE Series Profiles - Single-Phase Hybrid Solar Inverters (8-12kW)

The SPE 8000-12000 ES is a single-phase hybrid inverter with battery storage,
dual MPPT trackers, and grid-tied peak shaving capability. It supports parallel
operation for capacity expansion up to 108kW.

Key characteristics:
- 8-12kW capacity, single-phase hybrid
- Dual MPPT trackers (PV1 + PV2), max PV input 550VDC
- Two AC input terminals with integrated transfer switch
- Grid-tied with peak shaving capability
- Parallel operation support (up to 108kW)
- Dual outputs for smart load management

Register Layout Notes (from Issue #212 register scan analysis):
- Uses the same 0-97 base register range as SPF
- Register layout shares many SPF-compatible addresses but has key differences:
  * Regs 36/37: SPF uses these for ac_input_power; SPE produces garbage (429GW overflow)
    → These registers are excluded from this profile until the correct mapping is confirmed
  * Regs 64/65: SPF = AC discharge energy today; SPE = grid import energy today (confirmed 20.0 kWh)
  * Regs 66/67: SPF = AC discharge energy total; SPE = grid import energy total (confirmed 855.2 kWh)
  * Regs 85/86: SPF = op discharge energy today; SPE = load energy today (confirmed 21.3 kWh)
  * Regs 87/88: SPF = op discharge energy total; SPE = load energy total (confirmed 1028.3 kWh)
  * Regs 92-97: Generator registers — SPE has no generator input, all zero
- offgrid_protocol flag prevents reading VPP registers (30000+) which return garbage on this firmware

DTC Identification:
- This device returned DTC 64541 (unknown, not in standard mapping) in the Issue #212 scan
- Auto-detection falls back to legacy range analysis for this device
- See auto_detection.py for manual profile override instructions

Battery Power Sign Convention:
Same as SPF — hardware reports inverted convention (positive=discharge, negative=charge).
Negative scale (-0.1) on registers 77/78 converts to standard HA convention.
"""

from .spf import SPF_3000_6000_ES_PLUS

# Build SPE input registers by modifying the SPF base
# All confirmed register mappings are validated against Issue #212 daytime scan
# cross-referenced with actual entity values from the XLSX file
_spe_input_regs = dict(SPF_3000_6000_ES_PLUS['input_registers'])

# ── Remove registers that are wrong or absent on SPE ──────────────────────────

# Regs 36/37: SPF maps these as ac_input_power_high/low but SPE produces 429GW overflow.
# The 32-bit value appears to be a signed grid power register that the coordinator
# interprets as unsigned, yielding 0xFFFFFFE4 → 4,294,966,436 × 0.1 = 429,496,643.6W.
# Excluded until the correct signed semantics are confirmed from a cleaner scan.
for _addr in (36, 37):
    _spe_input_regs.pop(_addr, None)

# Regs 92-97: Generator discharge energy, generator power, generator voltage.
# SPE has no generator input port — these registers are all zero and inapplicable.
for _addr in (92, 93, 94, 95, 96, 97):
    _spe_input_regs.pop(_addr, None)

# ── Remap energy registers: SPF names are semantically wrong for SPE ──────────

# Regs 64/65: SPF labels these "AC discharge energy today" (battery-to-load via inverter).
# On SPE these registers track GRID IMPORT energy today.
# Confirmed: scan raw 200 × 0.1 = 20.0 kWh, actual grid import = 19.8 kWh ✓
_spe_input_regs.update({
    64: {
        'name': 'ac_discharge_energy_today_high', 'scale': 1, 'unit': '', 'pair': 65,
        'desc': 'Grid import energy today (HIGH word) [SPE: different semantics from SPF at same address]',
    },
    65: {
        'name': 'ac_discharge_energy_today_low', 'scale': 1, 'unit': '', 'pair': 64,
        'combined_scale': 0.1, 'combined_unit': 'kWh',
        'desc': 'Grid import energy today (LOW word). Confirmed ≈ 20.0 kWh (#212)',
    },
})

# Regs 66/67: SPF labels these "AC discharge energy total".
# On SPE these registers track GRID IMPORT energy total (lifetime).
# Confirmed: scan raw 8552 × 0.1 = 855.2 kWh, actual = 855.2 kWh ✓
_spe_input_regs.update({
    66: {
        'name': 'ac_discharge_energy_total_high', 'scale': 1, 'unit': '', 'pair': 67,
        'desc': 'Grid import energy total (HIGH word)',
    },
    67: {
        'name': 'ac_discharge_energy_total_low', 'scale': 1, 'unit': '', 'pair': 66,
        'combined_scale': 0.1, 'combined_unit': 'kWh',
        'desc': 'Grid import energy total (LOW word). Confirmed 855.2 kWh (#212)',
    },
})

# Regs 85/86: SPF labels these "operational discharge energy today".
# On SPE these registers track LOAD ENERGY consumed today (all sources: PV + grid + battery).
# Confirmed: scan raw 213 × 0.1 = 21.3 kWh, actual load today = 20.9 kWh ✓
_spe_input_regs.update({
    85: {
        'name': 'load_energy_today_high', 'scale': 1, 'unit': '', 'pair': 86,
        'desc': 'Load energy today (HIGH word)',
    },
    86: {
        'name': 'load_energy_today_low', 'scale': 1, 'unit': '', 'pair': 85,
        'combined_scale': 0.1, 'combined_unit': 'kWh',
        'desc': 'Load energy today (LOW word). Confirmed 21.3 kWh actual 20.9 kWh (#212)',
    },
})

# Regs 87/88: SPF labels these "operational discharge energy total".
# On SPE these registers track LOAD ENERGY consumed total (lifetime).
# Confirmed: scan raw 10283 × 0.1 = 1028.3 kWh, actual load total = 1027.9 kWh ✓
_spe_input_regs.update({
    87: {
        'name': 'load_energy_total_high', 'scale': 1, 'unit': '', 'pair': 88,
        'desc': 'Load energy total (HIGH word)',
    },
    88: {
        'name': 'load_energy_total_low', 'scale': 1, 'unit': '', 'pair': 87,
        'combined_scale': 0.1, 'combined_unit': 'kWh',
        'desc': 'Load energy total (LOW word). Confirmed 1028.3 kWh actual 1027.9 kWh (#212)',
    },
})

SPE_8000_12000_ES = {
    'name': 'SPE 8000-12000 ES',
    'description': 'Single-phase hybrid inverter with battery storage (8-12kW)',
    'notes': (
        'Uses SPF-compatible 0-97 register range with key remapping. '
        'Regs 64/65 = grid import energy (not AC discharge), '
        'Regs 85-88 = load energy today/total (not operational discharge). '
        'Regs 36/37 (ac_input_power) excluded — produces 429GW overflow. '
        'No generator input. PV register validation pending daytime scan.'
    ),
    # NOTE: offgrid_protocol refers to the REGISTER LAYOUT (SPF-style 0-97),
    # not the inverter's grid capability. The SPE supports grid-tied operation
    # with peak shaving. This flag prevents reading VPP registers (30000+)
    # which return garbage data on this device firmware.
    'offgrid_protocol': True,
    'input_registers': _spe_input_regs,
    'holding_registers': {
        # Holding registers inherited from SPF — confirmed consistent with
        # entity values seen in Issue #212 scan (charge_current, battery_type,
        # ac_input_mode, output_config, charge_config all reading correctly).
        **SPF_3000_6000_ES_PLUS['holding_registers'],
    },
}

# Export register maps for import by __init__.py
SPE_REGISTER_MAPS = {
    'SPE_8000_12000_ES': SPE_8000_12000_ES,
}
