# tools/

Helper scripts for the `growatt_modbus` fork.

Most files here are one-off protocol importers and analysers
(`import_*_protocol.py`, `generate_protocol_docs.py`, `protocol_query.py`, ...).
The one operational script is `deploy_to_ha.py`.

---

## `deploy_to_ha.py` — deploy the fork to the Home Assistant machine

Pushes `custom_components/growatt_modbus/` from this repo onto the HA config
share, optionally making HACS record the upstream release as installed first,
and optionally restarting HA and checking that it came back healthy.

### Requirements

Python 3.12+, standard library only — **except** the two steps that talk to the
HA websocket API (`--hacs-update`, and the `system_log` fallback used by
`--restart`), which need the `websockets` package:

```bash
# no websocket needed
uv run python tools/deploy_to_ha.py --dry-run --deploy

# anything that uses the websocket API
uv run --with websockets python tools/deploy_to_ha.py --hacs-update --deploy --restart
```

### The token

The HA long-lived access token comes from the `HA_TOKEN` environment variable or
from `--token-file <path>` (first non-empty line). It is never printed, never
written into the repo, and never passed on a command line. Only `--hacs-update`
and `--restart` need it.

```bash
HA_TOKEN='...' uv run --with websockets python tools/deploy_to_ha.py --hacs-update --deploy --restart
```

### Order of operations

The script always runs its steps in this fixed order, no matter how the flags
are ordered on the command line:

```
backup  ->  hacs-update  ->  deploy | restore  ->  restart
```

**Backup is always first**, including when `--hacs-update` is requested. HACS
deletes the integration directory before it re-downloads, so a backup taken
after the HACS step would be a backup of upstream, not of the fork that was
running.

**The HACS download must precede the file overlay.** This is the whole reason
the two steps live in one script:

- HACS decides "update available" purely from `.storage/hacs.repositories`, by
  comparing `version_installed` with `last_version`. Writing the fork's files
  over `custom_components/growatt_modbus` does not touch that record, so HACS
  keeps offering the upgrade — and one accidental click replaces the fork with
  plain upstream.
- `hacs/repository/download` fixes the record honestly, but to do so it
  **wipes the directory**: HACS moves the existing directory to a temp backup,
  downloads the GitHub tag zipball for the requested version, and extracts only
  the `custom_components/growatt_modbus/` subtree into the now-empty target.
- Therefore: let HACS download upstream v1.8.14 first, then overlay the fork
  (a superset of that tag, manifest version 1.8.14) on top. Reversing the order
  throws the fork away.

`--restore` cannot be combined with `--deploy` or `--hacs-update`.

### Typical invocations

```bash
# 1. Always look first. Runs every check, writes nothing, calls nothing.
uv run python tools/deploy_to_ha.py --dry-run --hacs-update --deploy --restart

# 2. The real thing.
HA_TOKEN='...' uv run --with websockets python tools/deploy_to_ha.py \
    --hacs-update --deploy --restart

# 3. Fork-only redeploy (no HACS bookkeeping change).
HA_TOKEN='...' uv run python tools/deploy_to_ha.py --deploy --restart

# 4. Rollback.
HA_TOKEN='...' uv run python tools/deploy_to_ha.py \
    --restore backup-20260902-125622 --restart
```

### What each step does

| Step | Behaviour |
| --- | --- |
| `backup` | Copies the share's `custom_components/growatt_modbus` to `<share>/growatt_modbus_backups/backup-<YYYYmmdd-HHMMSS>/` and keeps the 5 newest. **Not** under `custom_components/` — HA would try to load a copy of the integration from there. Pruning only touches `backup-*`, so the older `bak-*` directories already on the share are left alone. |
| `hacs-update` | Connects to `ws://<host>:8123/api/websocket`, authenticates, sends `{"type": "hacs/repository/download", "repository": "1065036927", "version": "v1.8.14"}`, waits for the result, then re-reads `.storage/hacs.repositories` over SMB in a retry loop (HACS writes it asynchronously) until `version_installed == "v1.8.14"`. Exits non-zero if it cannot confirm — deliberately *before* the overlay, so the fork is never written on top of an unknown state. |
| `deploy` | Mirror-copies the repo's `custom_components/growatt_modbus` onto the share: copies every file except `__pycache__/` and `*.pyc`, deletes files on the share the source does not have, removes `__pycache__` directories, then SHA256-verifies every file and fails loudly on any mismatch. Prints the manifest version deployed. |
| `restore` | The same mirror + verify, from a backup directory back onto the share. |
| `restart` | `POST /api/services/homeassistant/restart`, polls `GET /api/` until it answers (5 min timeout), prints any log lines mentioning `growatt_modbus`, then checks `sensor.growatt_inverter_mode` and `sensor.growatt_battery_battery_soc`. Exits non-zero if either entity is missing or `unavailable`/`unknown`. |

### Notes and gotchas

- **`GET /api/error_log` is gone.** On HA 2026.8.3 it returns `404`, and this
  installation writes no `home-assistant.log` to the config share. The script
  tries `/api/error_log` first and falls back to the `system_log/list`
  websocket command, which returns level, logger name, message and traceback.
  That fallback needs `websockets`; without it the script says so and tells you
  to check the log manually rather than silently reporting "no errors".
- **The restart POST usually drops the connection.** HA tears down the HTTP
  server while shutting down, so a `URLError` from that call is treated as a
  successful restart, not a failure. The authoritative check is the `GET /api/`
  poll that follows.
- **Git Bash eats one backslash of a UNC path** passed as an argument, turning
  `\\host\share\x` into `\host\share\x`. For `--restore`, pass just the backup
  directory name (`backup-20260902-125622`) and the script resolves it under the
  backup root; on failure it lists what is available. For `--share-root`, use
  forward slashes (`//192.168.33.167/config`) or rely on the default.
- **Legacy `bak-*` backups are not clean mirrors.** At least one contains a
  `RESTORE_THIS_BACKUP.ps1` helper, which a `--restore` would faithfully copy
  into the integration directory. Harmless to HA (only `.py` is loaded) but
  worth deleting afterwards. Backups the script takes itself do not have this.
- The script never reads a token from `apps.yaml` or anywhere else in the
  repo, and never creates files outside the backup root and the integration
  directory.

### Verifying afterwards

The HACS record and the deployed files are two independent facts. After a run,
`version_installed` in `.storage/hacs.repositories` should read `v1.8.14` (HACS
stops offering the update) while
`custom_components/growatt_modbus/manifest.json` reads the fork's version — the
script prints both.
