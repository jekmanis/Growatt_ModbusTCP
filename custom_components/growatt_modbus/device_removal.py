"""Let Home Assistant delete devices this integration no longer creates.

Without `async_remove_config_entry_device` on the integration module, Home Assistant
offers no "Delete" button for any of its devices and the device-registry API refuses the
removal too. That leaves leftovers from older device layouts stuck forever - the parent
inverter was registered as `(growatt_modbus, "<entry_id>")` before device structure v2,
and installs that predate it can still carry that device with nothing attached.

The hook is deliberately conservative. Wrongly deleting a live device takes its entities
with it, which is far worse than leaving clutter, so anything that is not clearly stale is
refused:

  - any identifier that `get_device_info()` still builds for this entry -> keep
  - any identifier from another domain or another config entry -> keep
  - any attached entity with a live (non-restored) state -> keep
  - an error while deciding -> keep

The live identifiers are derived from `const.device_identifier()` over
`const.ALL_DEVICE_TYPES` - the same helper `get_device_info()` builds DeviceInfo from -
rather than restated here, so the two cannot drift apart.

Kept out of `__init__.py`, which re-exports the hook, so the decision can be tested
without Home Assistant installed.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from .const import ALL_DEVICE_TYPES, DOMAIN, device_identifier

_LOGGER = logging.getLogger(__name__)


def current_device_identifiers(entry_id: str) -> frozenset:
    """Every device identifier the integration currently creates for this entry."""
    return frozenset(device_identifier(entry_id, t) for t in ALL_DEVICE_TYPES)


def _issued_by_entry(identifier: Any, entry_id: str) -> bool:
    """True for an identifier of our domain naming this entry, in any format past or present.

    Formats seen so far: the bare entry_id (parent before device structure v2) and
    `<entry_id>_<device_type>` (everything since).
    """
    try:
        domain, value = identifier
    except (TypeError, ValueError):
        return False
    return (
        domain == DOMAIN
        and isinstance(value, str)
        and (value == entry_id or value.startswith(f"{entry_id}_"))
    )


def removal_verdict(
    entry_id: str,
    device_config_entries: Iterable[str],
    identifiers: Iterable[Any],
    live_entity_ids: Iterable[str],
) -> tuple[bool, str]:
    """Decide whether a device may be removed from this entry. Returns (allowed, reason)."""
    if entry_id not in set(device_config_entries or ()):
        return False, "device is not attached to this config entry"

    identifiers = set(identifiers or ())
    if not identifiers:
        return False, "device has no identifiers, so it cannot be shown to be stale"

    still_created = sorted(str(i) for i in identifiers & current_device_identifiers(entry_id))
    if still_created:
        return False, f"the integration still creates this device ({', '.join(still_created)})"

    foreign = sorted(str(i) for i in identifiers if not _issued_by_entry(i, entry_id))
    if foreign:
        return False, (
            "device carries identifiers this entry did not issue "
            f"({', '.join(foreign)})"
        )

    live = sorted(live_entity_ids or ())
    if live:
        return False, f"entities are still live on this device ({', '.join(live)})"

    return True, "no current identifier and no live entities; stale device from an older layout"


def _live_entity_ids(hass: Any, device_id: str) -> list[str]:
    """Entities on the device that something is currently providing.

    An entity with no state is disabled or was never added this run; one whose state
    carries `restored: True` is Home Assistant's placeholder for a registry entry no
    platform claimed. Neither is provided by the loaded integration. Anything else -
    including a plain `unavailable` - is treated as live.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    live: list[str] = []
    for entity in er.async_entries_for_device(
        registry, device_id, include_disabled_entities=True
    ):
        state = hass.states.get(entity.entity_id)
        if state is None:
            continue
        if state.attributes.get("restored", False):
            continue
        live.append(entity.entity_id)
    return live


async def async_remove_config_entry_device(hass: Any, config_entry: Any, device_entry: Any) -> bool:
    """Home Assistant hook: may `device_entry` be removed from `config_entry`?"""
    entry_id = config_entry.entry_id
    label = getattr(device_entry, "name_by_user", None) or getattr(device_entry, "name", None)
    try:
        allowed, reason = removal_verdict(
            entry_id,
            device_entry.config_entries,
            device_entry.identifiers,
            _live_entity_ids(hass, device_entry.id),
        )
    except Exception as err:  # noqa: BLE001 - refusing is always the safe answer
        _LOGGER.warning(
            "Refusing removal of device %s (%s): could not evaluate it (%s)",
            label, getattr(device_entry, "id", "?"), err,
        )
        return False

    _LOGGER.info(
        "%s removal of device %s (%s, identifiers %s): %s",
        "Allowing" if allowed else "Refusing",
        label, device_entry.id, sorted(map(str, device_entry.identifiers or ())), reason,
    )
    return allowed
