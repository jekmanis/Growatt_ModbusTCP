#!/usr/bin/env python3
"""Deploy the growatt_modbus fork to the Home Assistant machine.

Python 3.12+, standard library only for everything except ``--hacs-update``
(and the ``system_log`` fallback in ``--restart``), which need a websocket
client.  Run it like this::

    # everything except --hacs-update / the system_log fallback
    uv run python tools/deploy_to_ha.py --dry-run --deploy --restart

    # anything that talks to the HA websocket API
    uv run --with websockets python tools/deploy_to_ha.py --hacs-update --deploy --restart

The HA long-lived token is read from the ``HA_TOKEN`` environment variable or
from ``--token-file <path>`` (first non-empty line).  It is never printed, never
written to the repository and never placed on a command line.

Order of operations (fixed, regardless of flag order on the command line):

    1. backup      - always first, so a failed HACS download is recoverable
    2. hacs-update
    3. deploy (or restore)
    4. restart

The HACS download MUST run before the file overlay: HACS deletes the whole
``custom_components/growatt_modbus`` directory and re-extracts the upstream tag
zipball into it.  Overlaying the fork first and then letting HACS download would
throw the fork away.

Why the HACS step exists at all: HACS decides "update available" by comparing
``version_installed`` against ``last_version`` in ``.storage/hacs.repositories``.
Writing the fork's files over the directory does not change that record, so HACS
would keep offering the upgrade - and one accidental click would replace the
fork with plain upstream.  Making HACS actually download v1.8.14 sets
``version_installed`` honestly; the fork (a superset of that tag) then goes on
top.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import http.client
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "192.168.33.167"
DEFAULT_PORT = 8123
DEFAULT_SHARE_ROOT = r"\\192.168.33.167\config"
DEFAULT_HACS_REPO_ID = "1065036927"
DEFAULT_HACS_VERSION = "v1.8.14"

INTEGRATION_DIR_NAME = "growatt_modbus"
BACKUP_DIR_NAME = "growatt_modbus_backups"
BACKUP_PREFIX = "backup-"
BACKUPS_TO_KEEP = 5

VERIFY_ENTITIES = (
    "sensor.growatt_inverter_mode",
    "sensor.growatt_battery_battery_soc",
)

EXCLUDED_DIR_NAMES = {"__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

RESTART_POLL_TIMEOUT = 300.0
RUNNING_WAIT_TIMEOUT = 180.0   # /api/config state -> RUNNING
ENTITY_CHECK_TIMEOUT = 120.0   # retry the entity check this long after RUNNING
ENTITY_CHECK_INTERVAL = 15.0
RESTART_POLL_INTERVAL = 5.0
HACS_DOWNLOAD_TIMEOUT = 600.0
HACS_STORAGE_RETRIES = 30
HACS_STORAGE_INTERVAL = 2.0


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

_DRY_RUN = False


def log(msg: str = "") -> None:
    if msg:
        print("[{0:%H:%M:%S}] {1}".format(datetime.now(), msg), flush=True)
    else:
        print(flush=True)


def step(msg: str) -> None:
    log()
    log("=== {0} ===".format(msg))


def plan(msg: str) -> None:
    """Log an action, prefixed when nothing is actually going to happen."""
    log("{0}{1}".format("DRY-RUN would: " if _DRY_RUN else "", msg))


class DeployError(RuntimeError):
    """Fatal, already-explained failure."""


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #


def load_token(token_file):
    if token_file:
        path = Path(token_file).expanduser()
        if not path.is_file():
            raise DeployError("--token-file not found: {0}".format(path))
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip().strip('"').strip("'")
            if line:
                return line
        raise DeployError("--token-file is empty: {0}".format(path))

    token = (os.environ.get("HA_TOKEN") or "").strip()
    if not token:
        raise DeployError(
            "No HA token. Set the HA_TOKEN environment variable or pass "
            "--token-file <path>. (The token is never read from the repo.)"
        )
    return token


# --------------------------------------------------------------------------- #
# file helpers
# --------------------------------------------------------------------------- #


def relative_files(root):
    """Map relative POSIX path -> absolute path, skipping caches and bytecode."""
    found = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        found[rel.as_posix()] = path
    return found


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_version(root):
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return "<no manifest.json>"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "<unset>"))
    except (OSError, ValueError) as exc:
        return "<unreadable: {0}>".format(exc)


def find_cache_dirs(target):
    if not target.is_dir():
        return []
    return sorted(p for p in target.rglob("*") if p.is_dir() and p.name in EXCLUDED_DIR_NAMES)


def mirror(source, target, label):
    """Mirror ``source`` onto ``target``, then SHA256-verify every file.

    Copies all files except ``__pycache__``/bytecode, deletes files on the
    target that the source does not have, and removes ``__pycache__``
    directories from the target.
    """
    if not source.is_dir():
        raise DeployError("Source directory does not exist: {0}".format(source))

    src_files = relative_files(source)
    if not src_files:
        raise DeployError("Source directory has no deployable files: {0}".format(source))
    dst_files = relative_files(target) if target.is_dir() else {}

    to_copy = sorted(src_files)
    to_delete = sorted(set(dst_files) - set(src_files))
    caches = find_cache_dirs(target)

    log("{0}: {1}".format(label, source))
    log("        -> {0}".format(target))
    log("  manifest version in source: {0}".format(manifest_version(source)))
    log("  {0} file(s) to copy, {1} stale file(s) to delete, {2} __pycache__ dir(s) to remove"
        .format(len(to_copy), len(to_delete), len(caches)))
    for rel in to_delete:
        log("    stale (will be deleted): {0}".format(rel))
    for cache in caches:
        log("    cache (will be removed): {0}".format(cache))

    if _DRY_RUN:
        plan("copy {0} file(s) and SHA256-verify each one".format(len(to_copy)))
        for rel in to_copy[:10]:
            log("      + {0}".format(rel))
        if len(to_copy) > 10:
            log("      ... and {0} more".format(len(to_copy) - 10))
        return

    target.mkdir(parents=True, exist_ok=True)
    for rel in to_copy:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_files[rel], dst)
    for rel in to_delete:
        (target / rel).unlink(missing_ok=True)
    for cache in caches:
        shutil.rmtree(cache, ignore_errors=True)

    log("  copied {0} file(s); verifying SHA256...".format(len(to_copy)))
    mismatches = []
    for rel in to_copy:
        dst = target / rel
        if not dst.is_file():
            mismatches.append("{0}: MISSING on target".format(rel))
            continue
        if sha256(src_files[rel]) != sha256(dst):
            mismatches.append("{0}: SHA256 MISMATCH".format(rel))
    for rel in sorted(set(relative_files(target)) - set(src_files)):
        mismatches.append("{0}: still present on target after mirror".format(rel))

    if mismatches:
        for line in mismatches:
            log("  FAIL {0}".format(line))
        raise DeployError(
            "{0} verification failed: {1} problem(s). The target directory is in an "
            "UNKNOWN state - roll back with --restore.".format(label, len(mismatches))
        )

    log("  OK: {0} file(s) verified byte-identical".format(len(to_copy)))
    log("  deployed manifest version: {0}".format(manifest_version(target)))


# --------------------------------------------------------------------------- #
# steps
# --------------------------------------------------------------------------- #


def do_backup(integration_dir, backup_root):
    step("BACKUP")
    if not integration_dir.is_dir():
        log("  nothing to back up, {0} does not exist".format(integration_dir))
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backup_root / (BACKUP_PREFIX + stamp)
    files = relative_files(integration_dir)
    log("  source: {0}".format(integration_dir))
    log("  {0} file(s), manifest version {1}".format(len(files), manifest_version(integration_dir)))
    plan("create backup at {0}".format(dest))

    if not _DRY_RUN:
        dest.parent.mkdir(parents=True, exist_ok=True)
        for rel, src in files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        log("  backup written: {0}".format(dest))

    # Prune - only touches our own backup-<ts> directories, so any legacy naming
    # scheme sharing the folder is left alone.
    existing = sorted(
        (p for p in backup_root.glob(BACKUP_PREFIX + "*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    if _DRY_RUN:
        existing = [dest] + existing
    stale = existing[BACKUPS_TO_KEEP:]
    log("  {0} '{1}*' backup(s) would exist, keeping the {2} newest"
        .format(len(existing), BACKUP_PREFIX, BACKUPS_TO_KEEP))
    for old in stale:
        plan("prune old backup {0}".format(old))
        if not _DRY_RUN:
            shutil.rmtree(old, ignore_errors=True)
    if not stale:
        log("  nothing to prune")
    return dest


def resolve_backup(value, backup_root):
    """Resolve --restore to a directory.

    A bare name (``backup-20260902-125622``) is looked up under the backup root.
    That is the recommended form: Git Bash silently collapses ``\\\\host\\share``
    to ``\\host\\share`` when it is passed as an argument, which turns a full UNC
    path into a nonexistent one.  Forward slashes (``//host/share/...``) survive.
    """
    candidates = []
    raw = Path(value)
    if raw.is_dir():
        return raw
    candidates.append(str(raw))

    named = backup_root / value
    if named.is_dir():
        log("  resolved {0!r} under the backup root".format(value))
        return named
    candidates.append(str(named))

    available = sorted(
        (p.name for p in backup_root.glob("*") if p.is_dir()), reverse=True
    ) if backup_root.is_dir() else []
    hint = "\n  available in {0}:\n    {1}".format(
        backup_root, "\n    ".join(available[:15]) or "(none)"
    )
    raise DeployError(
        "Backup directory not found. Tried:\n    {0}{1}\n"
        "Pass just the directory name, or use forward slashes for a full path "
        "(Git Bash eats one leading backslash of a UNC path).".format(
            "\n    ".join(candidates), hint)
    )


def read_hacs_storage(share_root, repo_id):
    path = share_root / ".storage" / "hacs.repositories"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (data.get("data") or {}).get(repo_id)


def do_hacs_update(args, token, share_root):
    step("HACS UPDATE")
    payload = {
        "type": "hacs/repository/download",
        "repository": args.hacs_repo_id,
        "version": args.hacs_version,
    }
    ws_url = "ws://{0}:{1}/api/websocket".format(args.host, args.port)
    log("  websocket: {0}".format(ws_url))
    log("  command:   {0}".format(json.dumps(dict([("id", "<n>")] + list(payload.items())))))

    current = read_hacs_storage(share_root, args.hacs_repo_id)
    if current is None:
        log("  WARNING: repository id {0} not found in .storage/hacs.repositories "
            "(cannot pre-check)".format(args.hacs_repo_id))
    else:
        log("  current storage record: full_name={0!r} version_installed={1!r} "
            "installed_commit={2!r} last_version={3!r}".format(
                current.get("full_name"), current.get("version_installed"),
                current.get("installed_commit"), current.get("last_version")))
        if current.get("version_installed") == args.hacs_version:
            log("  already at {0}; the download would be a no-op re-extract"
                .format(args.hacs_version))

    log("  NOTE: HACS deletes the whole integration directory and re-extracts the")
    log("        upstream tag zipball into it. The fork overlay must run AFTER this.")

    if _DRY_RUN:
        plan("authenticate and send the command above, then wait up to {0:.0f}s for its result"
             .format(HACS_DOWNLOAD_TIMEOUT))
        plan("re-read .storage/hacs.repositories (up to {0} times, {1:.0f}s apart) and require "
             "version_installed == {2!r}".format(
                 HACS_STORAGE_RETRIES, HACS_STORAGE_INTERVAL, args.hacs_version))
        return

    _hacs_download(ws_url, token, payload)

    log("  confirming version_installed == {0!r} in .storage (HACS writes asynchronously)..."
        .format(args.hacs_version))
    for attempt in range(1, HACS_STORAGE_RETRIES + 1):
        record = read_hacs_storage(share_root, args.hacs_repo_id)
        installed = (record or {}).get("version_installed")
        if installed == args.hacs_version:
            log("  confirmed after {0} read(s): version_installed={1!r} installed_commit={2!r}"
                .format(attempt, installed, (record or {}).get("installed_commit")))
            return
        if attempt == 1 or attempt % 5 == 0:
            log("    attempt {0}/{1}: version_installed={2!r}"
                .format(attempt, HACS_STORAGE_RETRIES, installed))
        time.sleep(HACS_STORAGE_INTERVAL)

    raise DeployError(
        "HACS did not record version_installed == {0!r} within {1:.0f}s. Aborting before the "
        "overlay so the fork is not written on top of an unknown state.".format(
            args.hacs_version, HACS_STORAGE_RETRIES * HACS_STORAGE_INTERVAL)
    )


def _require_websockets():
    try:
        import websockets
    except ImportError as exc:
        raise DeployError(
            "The 'websockets' package is required for this step. Re-run with:\n"
            "  uv run --with websockets python tools/deploy_to_ha.py ..."
        ) from exc
    return websockets


def _ws_call(ws_url, token, messages, timeout):
    """Authenticate, send each message with an incrementing id, return the results."""
    websockets = _require_websockets()
    import asyncio

    async def run():
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            greeting = json.loads(await ws.recv())
            if greeting.get("type") != "auth_required":
                raise DeployError("Unexpected websocket greeting: {0}".format(greeting))
            await ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise DeployError(
                    "Websocket authentication failed ({0}): {1}".format(
                        auth.get("type"), auth.get("message", ""))
                )
            log("  authenticated (HA {0})".format(auth.get("ha_version")))

            results = []
            for index, message in enumerate(messages, start=1):
                outgoing = dict(message)
                outgoing["id"] = index
                await ws.send(json.dumps(outgoing))
                log("  sent {0} (id={1}), waiting up to {2:.0f}s...".format(
                    message["type"], index, timeout))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    reply = json.loads(raw)
                    if reply.get("id") != index or reply.get("type") != "result":
                        continue  # progress/subscription events are not our answer
                    results.append(reply)
                    break
            return results

    try:
        return asyncio.run(run())
    except asyncio.TimeoutError as exc:
        raise DeployError(
            "Timed out waiting for a websocket result after {0:.0f}s".format(timeout)
        ) from exc
    except DeployError:
        raise
    except Exception as exc:
        raise DeployError("Websocket call failed: {0}: {1}".format(type(exc).__name__, exc)) from exc


def _hacs_download(ws_url, token, payload):
    reply = _ws_call(ws_url, token, [payload], timeout=HACS_DOWNLOAD_TIMEOUT)[0]
    if not reply.get("success"):
        error = reply.get("error") or {}
        raise DeployError("HACS download failed: {0}: {1}".format(
            error.get("code"), error.get("message")))
    log("  HACS reported the download succeeded")


# --------------------------------------------------------------------------- #
# REST helpers
# --------------------------------------------------------------------------- #


def _request(base, path, token, method="GET", body=None, timeout=30.0):
    request = urllib.request.Request(
        base + path,
        method=method,
        data=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def do_restart(args, token):
    step("RESTART HOME ASSISTANT")
    base = "http://{0}:{1}".format(args.host, args.port)
    ws_url = "ws://{0}:{1}/api/websocket".format(args.host, args.port)
    plan("POST {0}/api/services/homeassistant/restart".format(base))

    if _DRY_RUN:
        plan("poll GET {0}/api/ until it answers (timeout {1:.0f}s)".format(
            base, RESTART_POLL_TIMEOUT))
        plan("collect {0} log lines: GET /api/error_log, falling back to the "
             "system_log/list websocket command".format(INTEGRATION_DIR_NAME))
        for entity in VERIFY_ENTITIES:
            plan("GET {0}/api/states/{1} and require a usable state".format(base, entity))
        return True

    try:
        status, body = _request(
            base, "/api/services/homeassistant/restart", token,
            method="POST", body=b"{}", timeout=30.0,
        )
        log("  restart request returned HTTP {0}".format(status))
        if status not in (200, 201):
            raise DeployError("Restart was refused: HTTP {0}: {1!r}".format(status, body[:200]))
    except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError) as exc:
        # HA regularly drops the connection while shutting down (seen live on
        # 2026-09-02 as http.client.RemoteDisconnected, which is NOT a URLError);
        # that is a successful restart, not a failure.
        log("  connection dropped while restarting ({0!r}) - expected, continuing".format(exc))

    log("  waiting for the API to come back (timeout {0:.0f}s)...".format(RESTART_POLL_TIMEOUT))
    started = time.monotonic()
    deadline = started + RESTART_POLL_TIMEOUT
    time.sleep(RESTART_POLL_INTERVAL)
    back = False
    while time.monotonic() < deadline:
        try:
            status, _ = _request(base, "/api/", token, timeout=10.0)
            if status == 200:
                log("  API is back after ~{0:.0f}s".format(time.monotonic() - started))
                back = True
                break
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(RESTART_POLL_INTERVAL)
    if not back:
        raise DeployError(
            "Home Assistant did not answer within {0:.0f}s after the restart".format(
                RESTART_POLL_TIMEOUT)
        )

    _wait_for_running(base, token)
    _report_integration_errors(base, ws_url, token)
    # Entities are registered while HA is still starting; a 404 in the first
    # minute after the API answers is timing, not a broken integration (seen
    # live on 2026-09-02: API back after 26 s, entities present at ~45 s).
    deadline = time.monotonic() + ENTITY_CHECK_TIMEOUT
    while True:
        if _check_entities(base, token):
            return True
        if time.monotonic() >= deadline:
            return False
        log("  entities not usable yet; retrying in {0:.0f}s".format(ENTITY_CHECK_INTERVAL))
        time.sleep(ENTITY_CHECK_INTERVAL)


def _wait_for_running(base, token):
    """Block until /api/config reports state RUNNING (or the timeout passes)."""
    deadline = time.monotonic() + RUNNING_WAIT_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        try:
            status, body = _request(base, "/api/config", token, timeout=10.0)
            if status == 200:
                last = json.loads(body).get("state")
                if last == "RUNNING":
                    log("  Home Assistant reports state RUNNING")
                    return
        except (urllib.error.URLError, http.client.HTTPException, OSError):
            pass
        time.sleep(RESTART_POLL_INTERVAL)
    log("  WARNING: state is still {0!r} after {1:.0f}s; checking entities anyway".format(
        last, RUNNING_WAIT_TIMEOUT))


def _report_integration_errors(base, ws_url, token):
    log()
    log("  --- log lines mentioning {0} ---".format(INTEGRATION_DIR_NAME))
    status, body = _request(base, "/api/error_log", token, timeout=60.0)
    if status == 200:
        lines = body.decode("utf-8", "replace").splitlines()
        hits = [line for line in lines if INTEGRATION_DIR_NAME in line]
        if hits:
            for line in hits:
                log("    {0}".format(line))
        else:
            log("    none in {0} log line(s)".format(len(lines)))
        return

    # HA 2026.8.3 does not serve /api/error_log (404). system_log/list is the
    # supported replacement and carries level, message and traceback.
    log("    GET /api/error_log -> HTTP {0}; falling back to system_log/list".format(status))
    try:
        reply = _ws_call(ws_url, token, [{"type": "system_log/list"}], timeout=60.0)[0]
    except DeployError as exc:
        log("    could not read the system log: {0}".format(exc))
        log("    check the Home Assistant log manually before trusting this deploy")
        return
    if not reply.get("success"):
        log("    system_log/list failed: {0}".format(reply.get("error")))
        return

    entries = reply.get("result") or []
    hits = [e for e in entries if INTEGRATION_DIR_NAME in json.dumps(e, default=str)]
    if not hits:
        log("    none in {0} system_log entry/entries".format(len(entries)))
        return
    for entry in hits:
        log("    [{0}] {1}: {2} (count={3})".format(
            entry.get("level"), entry.get("name"), entry.get("message"), entry.get("count")))
        for line in (entry.get("exception") or "").splitlines():
            log("      {0}".format(line))


def _check_entities(base, token):
    log()
    log("  --- entity check ---")
    ok = True
    for entity in VERIFY_ENTITIES:
        status, body = _request(base, "/api/states/" + entity, token, timeout=30.0)
        if status != 200:
            log("    FAIL {0}: HTTP {1} (entity missing?)".format(entity, status))
            ok = False
            continue
        state = json.loads(body)
        value = state.get("state")
        updated = state.get("last_updated")
        if value in ("unavailable", "unknown", None):
            log("    FAIL {0}: state={1!r} last_updated={2}".format(entity, value, updated))
            ok = False
        else:
            log("    OK   {0}: state={1!r} last_updated={2}".format(entity, value, updated))
    return ok


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def build_parser():
    parser = argparse.ArgumentParser(
        description="Deploy the growatt_modbus fork to Home Assistant.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Order is always: backup -> hacs-update -> deploy/restore -> restart.\n"
            "The token comes from $HA_TOKEN or --token-file and is never printed."
        ),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="print everything that would happen; write and call nothing")
    parser.add_argument("--deploy", action="store_true",
                        help="mirror the repo's custom_components/growatt_modbus onto the share")
    parser.add_argument("--hacs-update", action="store_true",
                        help="make HACS download the pinned upstream version first")
    parser.add_argument("--restart", action="store_true",
                        help="restart HA, then check the log and the entities")
    parser.add_argument("--restore", metavar="BACKUP_DIR",
                        help="mirror a backup directory back onto the share (rollback)")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the backup step (not recommended)")

    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--share-root", default=DEFAULT_SHARE_ROOT,
                        help="HA config share root (default: {0})".format(DEFAULT_SHARE_ROOT))
    parser.add_argument("--source", default=None,
                        help="source directory (default: <repo>/custom_components/growatt_modbus)")
    parser.add_argument("--token-file",
                        help="file whose first non-empty line is the HA long-lived token")
    parser.add_argument("--hacs-repo-id", default=DEFAULT_HACS_REPO_ID)
    parser.add_argument("--hacs-version", default=DEFAULT_HACS_VERSION)
    return parser


def main(argv=None):
    global _DRY_RUN

    args = build_parser().parse_args(argv)
    _DRY_RUN = args.dry_run

    if not (args.deploy or args.hacs_update or args.restart or args.restore):
        log("Nothing to do. Pass at least one of --deploy, --hacs-update, --restart, --restore.")
        return 2
    if args.restore and (args.deploy or args.hacs_update):
        log("--restore cannot be combined with --deploy or --hacs-update.")
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    if args.source:
        source = Path(args.source)
    else:
        source = repo_root / "custom_components" / INTEGRATION_DIR_NAME
    share_root = Path(args.share_root)
    integration_dir = share_root / "custom_components" / INTEGRATION_DIR_NAME
    backup_root = share_root / BACKUP_DIR_NAME

    step("PLAN")
    log("  mode:            {0}".format(
        "DRY RUN (nothing will be written or called)" if _DRY_RUN else "LIVE"))
    log("  repo:            {0}".format(repo_root))
    log("  source:          {0}".format(source))
    log("  share root:      {0}".format(share_root))
    log("  integration dir: {0}".format(integration_dir))
    log("  backup root:     {0}".format(backup_root))
    log("  HA:              http://{0}:{1}".format(args.host, args.port))
    order = [] if args.no_backup else ["backup"]
    if args.hacs_update:
        order.append("hacs-update({0}@{1})".format(args.hacs_repo_id, args.hacs_version))
    if args.deploy:
        order.append("deploy")
    if args.restore:
        order.append("restore({0})".format(args.restore))
    if args.restart:
        order.append("restart")
    log("  steps:           {0}".format(" -> ".join(order)))

    token = ""
    if args.hacs_update or args.restart:
        token = load_token(args.token_file)
        log("  token:           loaded ({0} chars, value never printed)".format(len(token)))

    if not share_root.is_dir():
        raise DeployError(
            "Share root is not reachable: {0}. Mount it in Explorer first, or pass "
            "--share-root.".format(share_root)
        )

    if args.no_backup:
        step("BACKUP")
        log("  SKIPPED (--no-backup)")
    else:
        do_backup(integration_dir, backup_root)

    if args.hacs_update:
        do_hacs_update(args, token, share_root)

    if args.deploy:
        step("DEPLOY")
        mirror(source, integration_dir, "deploy")

    if args.restore:
        step("RESTORE")
        backup_dir = resolve_backup(args.restore, backup_root)
        mirror(backup_dir, integration_dir, "restore")

    healthy = True
    if args.restart:
        healthy = do_restart(args, token)

    step("DONE")
    if _DRY_RUN:
        log("  dry run complete - nothing was written and no service was called")
        return 0
    if not healthy:
        log("  FAILED: Home Assistant restarted but the growatt_modbus entities are not usable.")
        log("  Roll back with: --restore {0} --restart".format(
            backup_root / (BACKUP_PREFIX + "<timestamp>")))
        return 1
    log("  completed successfully")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DeployError as exc:
        log()
        log("ERROR: {0}".format(exc))
        sys.exit(1)
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(130)
