#!/usr/bin/env python3
"""
Master import runner — imports all Growatt protocol Excel files into protocol_database.db.

Protocol set (4 distinct, non-redundant protocols):
  1. VPP_2.03              — Modern VPP protocol (30000+ register range)
                             Imported by: import_vpp_protocol.py
  2. RTU_Protocol_II_v1.39 — Definitive Protocol II (superset of V1.13/V1.20/V1.24)
                             Covers: WIT, SPH, SPA, MIN, MOD, MID, MAX, MAC
                             Imported by: import_wit_v139_protocol.py
  3. OffGrid_RTU_v0.26     — SPF off-grid/hybrid specific
                             Imported by: import_offgrid_v026_protocol.py
  4. PV_Modbus_v3.14       — Legacy PV-only string inverters (pre-2017)
                             Imported by: import_pv_v314_protocol.py

Usage:
  py tools/import_all_protocols.py              # Import all missing protocols
  py tools/import_all_protocols.py --force      # Re-import (skip existing protocols)
  py tools/import_all_protocols.py --stats      # Show database stats only

Note: VPP 2.03 must be imported first (it creates the schema).
      Run import_vpp_protocol.py manually if starting from scratch.
"""

import sys
import sqlite3
import subprocess
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / 'docs' / 'protocol_database.db'
TOOLS_DIR = Path(__file__).parent

IMPORT_SCRIPTS = [
    ('VPP_2.03',              'import_vpp_protocol.py'),
    ('RTU_Protocol_II_v1.39', 'import_wit_v139_protocol.py'),
    ('OffGrid_RTU_v0.26',     'import_offgrid_v026_protocol.py'),
    ('PV_Modbus_v3.14',       'import_pv_v314_protocol.py'),
]


def get_existing_protocols(db_path):
    """Return set of protocol names already in the database."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT name FROM protocols")
    names = {row[0] for row in cur.fetchall()}
    conn.close()
    return names


def show_stats(db_path):
    """Print database statistics."""
    if not db_path.exists():
        print("Database does not exist yet.")
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    print("\n" + "=" * 70)
    print("Protocol Database Statistics")
    print("=" * 70)

    cur.execute("SELECT id, name, version, applicable_models FROM protocols ORDER BY id")
    protocols = cur.fetchall()
    print(f"\nProtocols ({len(protocols)}):")
    for pid, name, version, models in protocols:
        cur.execute(
            "SELECT register_type, COUNT(*), MIN(address), MAX(address) "
            "FROM registers WHERE protocol_id=? GROUP BY register_type",
            (pid,)
        )
        reg_rows = cur.fetchall()
        print(f"\n  [{pid}] {name} (v{version})")
        print(f"       Models: {models}")
        for reg_type, cnt, min_a, max_a in reg_rows:
            print(f"       {reg_type:8}: {cnt:4d} registers  (addr {min_a}–{max_a})")

    total = cur.execute("SELECT COUNT(*) FROM registers").fetchone()[0]
    dtc = cur.execute("SELECT COUNT(*) FROM dtc_codes").fetchone()[0]
    print(f"\nTotals: {total} registers, {dtc} DTC codes")
    print("=" * 70)
    conn.close()


def run_import(script_name):
    """Run an import script and return True on success."""
    script_path = TOOLS_DIR / script_name
    if not script_path.exists():
        print(f"  ERROR: Script not found: {script_path}")
        return False

    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=False,
        text=True
    )
    return result.returncode == 0


def main():
    force = '--force' in sys.argv
    stats_only = '--stats' in sys.argv

    if stats_only:
        show_stats(DB_PATH)
        return 0

    existing = get_existing_protocols(DB_PATH)
    print(f"Database: {DB_PATH}")
    print(f"Existing protocols: {existing or '(none)'}\n")

    success_count = 0
    skip_count = 0
    fail_count = 0

    for protocol_name, script_name in IMPORT_SCRIPTS:
        if protocol_name in existing and not force:
            print(f"  SKIP  {protocol_name} (already imported)")
            skip_count += 1
            continue

        print(f"\n--- Importing {protocol_name} ---")
        ok = run_import(script_name)
        if ok:
            success_count += 1
        else:
            print(f"  FAILED: {script_name}")
            fail_count += 1

    print("\n" + "=" * 70)
    print(f"Done: {success_count} imported, {skip_count} skipped, {fail_count} failed")
    show_stats(DB_PATH)
    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
