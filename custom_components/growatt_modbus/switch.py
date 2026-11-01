"""Switch platform for Growatt Modbus Integration.

Upstream: one switch, remote on/off (the AC output, on off-grid models). Disabled by
default on every profile, so nobody turns an inverter off by accident - see
power_control.py for why each protocol family needs its own encoding.

Fork: two WIT switches on top - Grid Export (30201) and the Battery Optimizer kill switch
(30411 clear + input_boolean).
"""
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_REGISTER_MAP,
    REGISTER_MAPS,
    DEVICE_TYPE_GRID,
    DEVICE_TYPE_INVERTER,
    PROFILE_STATUS_MAP,
    WIT_REGISTER_MAPS,
)
from .coordinator import GrowattModbusCoordinator
from .entity import GrowattEntity
from .power_control import (
    PowerControl,
    PowerControlError,
    decode,
    resolve_power_control,
    running_from_status,
    set_power,
)

_LOGGER = logging.getLogger(__name__)

# Writable platform - serialise. See number.py for the reasoning.
PARALLEL_UPDATES = 1

# TOU periods register (30411) - setting to 0 clears all TOU schedules
VPP_TOU_NUM_PERIODS = 30411

# Export limit power rate register (30201) - 0=block, 100=allow
VPP_EXPORT_LIMIT_POWER_RATE = 30201

# Battery optimizer input_boolean entity ID
OPTIMIZER_ENTITY = "input_boolean.battery_optimizer_enabled"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Growatt Modbus switch entities."""
    # Coordinators live on entry.runtime_data since v1.6; hass.data[DOMAIN] now holds
    # only the shared-connection registry, so indexing it by entry_id is always a bug.
    coordinator = config_entry.runtime_data

    entities: list[SwitchEntity] = []

    # Upstream: remote on/off, when the profile has an on/off register.
    control = resolve_power_control(coordinator.modbus_client.register_map)
    if control is not None:
        entities.append(GrowattPowerSwitch(coordinator, config_entry, control))

    register_map_name = config_entry.data.get(CONF_REGISTER_MAP)
    register_map = REGISTER_MAPS.get(register_map_name, {})
    holding_registers = register_map.get('holding_registers', {})

    # The same tuple select.py, number.py and time.py gate on. Comparing against the
    # first map alone meant that on WIT_29900_50000TL3_XHU - which carries 30201 and
    # 30411 exactly like the 4000-15000 map - the mode sensor, the Mode Preset select and
    # the VPP numbers were created while these two switches silently were not. The
    # per-register checks below are what actually decides; the profile test only scopes
    # the branch.
    is_wit = str(register_map_name).upper() in WIT_REGISTER_MAPS

    if is_wit:
        wit_count = 0
        # Grid Export switch (register 30201)
        if VPP_EXPORT_LIMIT_POWER_RATE in holding_registers:
            entities.append(GrowattWitExportSwitch(coordinator, config_entry))
            wit_count += 1

        # Battery Optimizer switch (controls input_boolean + TOU clear)
        if VPP_TOU_NUM_PERIODS in holding_registers:
            entities.append(GrowattWitOptimizerSwitch(coordinator, config_entry))
            wit_count += 1

        if wit_count:
            entry_name = config_entry.data.get("name", config_entry.title)
            _LOGGER.info("Created %d WIT switch entities for %s", wit_count, entry_name)

    if entities:
        async_add_entities(entities)


class GrowattPowerSwitch(GrowattEntity, SwitchEntity):
    """Remote on/off.

    Where the profile marks the register as reading back (confirmed on a real unit), the
    state is read from the inverter each poll and the switch is an ordinary toggle.
    Elsewhere it shows the last command sent: V1.39 documents register 0 as write-only and
    VPP 30101 as not stored, so an unconfirmed read cannot be trusted to reflect the
    inverter, and `assumed_state` makes Home Assistant offer both actions instead.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_assumed_state = True
    _attr_icon = "mdi:power"

    def __init__(self, coordinator, config_entry: ConfigEntry, control: PowerControl) -> None:
        """Initialise the on/off switch."""
        super().__init__(
            coordinator,
            config_entry,
            unique_key="inverter_power",
            device_type=DEVICE_TYPE_INVERTER,
        )
        self._control = control
        self._attr_translation_key = "ac_output" if control.is_ac_output else "inverter_power"
        self._attr_is_on = None
        if control.readback:
            self._attr_assumed_state = False

    async def async_added_to_hass(self) -> None:
        """Ask the coordinator to start reading the register, where it reads back.

        Disabled entities are never added, so leaving the switch off costs no reads.
        """
        await super().async_added_to_hass()
        if self._control.readback:
            self.coordinator.enable_onoff_polling(self._control.register)

    @property
    def is_on(self) -> bool | None:
        if self._control.readback:
            return decode(self._control, self.coordinator.onoff_raw)
        client = self.coordinator.modbus_client
        data = self.coordinator.data
        if running_from_status(
            self._control,
            PROFILE_STATUS_MAP.get(getattr(client, "register_map_name", "") or "", "grid_tied"),
            bool(self.coordinator.last_update_success and self.coordinator.is_online),
            getattr(data, "status", None) if data is not None else None,
        ):
            return True
        return self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        try:
            value = await self.hass.async_add_executor_job(
                set_power, self.coordinator.modbus_client, self._control, on,
            )
        except PowerControlError as err:
            raise HomeAssistantError(f"Could not switch the inverter {'on' if on else 'off'}: {err}") from err

        _LOGGER.info(
            "Inverter %s: wrote %d (0x%04X) to holding register %d (%s)",
            "on" if on else "off", value, value, self._control.register, self._control.encoding,
        )
        if self._control.readback:
            await self.coordinator.async_request_refresh()
            return
        self._attr_is_on = on
        self.async_write_ha_state()


class GrowattWitExportSwitch(GrowattEntity, SwitchEntity):
    """Toggle grid export on/off (register 30201: 0=off, 100=on)."""

    _attr_icon = "mdi:transmission-tower-export"
    _attr_entity_category = EntityCategory.CONFIG
    # "Export", not "Grid Export". has_entity_name is inherited from GrowattEntity, so HA
    # composes the friendly name as device + entity, and this entity sits on the Grid
    # sub-device ("Growatt Grid") - "Grid Export" rendered as "Growatt Grid Grid Export"
    # where the pre-merge entity read "Growatt Grid Export".
    # The unique_id suffix must stay `grid_export_switch` - switch.growatt_grid_export
    # is an existing registry row.
    _attr_name = "Export"

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            coordinator, config_entry, "grid_export_switch", DEVICE_TYPE_GRID
        )

    @property
    def available(self) -> bool:
        """Unavailable while 30200-30201 has not been read.

        Same rule as GrowattGenericNumber/GrowattGenericSelect, and for the same reason:
        the VPP holding blocks are best-effort reads, GrowattData is rebuilt per poll, and
        a missed block leaves the dataclass default 0. Published as `off` that is not "we
        do not know" but "export is blocked" - a fabricated state for a limiter the
        inverter has not touched, which is the defect class that froze the mode sensor at
        "Passthrough". `vpp_export_limit_available` is set only on a successful read of
        the block.
        """
        if not super().available:
            return False
        data = self.coordinator.data
        return bool(data is not None and getattr(data, 'vpp_export_limit_available', False))

    @property
    def is_on(self) -> bool | None:
        """True if export is enabled (30201 > 0); None when the block was not read."""
        data = self.coordinator.data
        if data is None or not getattr(data, 'vpp_export_limit_available', False):
            return None
        return getattr(data, 'vpp_export_limit_power_rate', 0) > 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable export: write 30201=100."""
        try:
            success = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_register,
                VPP_EXPORT_LIMIT_POWER_RATE, 100,
            )
            if success:
                _LOGGER.info("[WIT] Grid export enabled (30201=100)")
                await self.coordinator.async_request_refresh()
            else:
                _LOGGER.error("[WIT] Failed to enable grid export")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("[WIT] Grid export enable failed: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable export: write 30201=0."""
        try:
            success = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_register,
                VPP_EXPORT_LIMIT_POWER_RATE, 0,
            )
            if success:
                _LOGGER.info("[WIT] Grid export disabled (30201=0)")
                await self.coordinator.async_request_refresh()
            else:
                _LOGGER.error("[WIT] Failed to disable grid export")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("[WIT] Grid export disable failed: %s", err)


class GrowattWitOptimizerSwitch(GrowattEntity, SwitchEntity):
    """Enable/disable battery optimizer + TOU schedules.

    OFF: Clears TOU periods (30411=0) AND turns off input_boolean.battery_optimizer_enabled
    ON:  Turns on input_boolean.battery_optimizer_enabled (optimizer re-syncs TOUs on next cycle)
    """

    _attr_icon = "mdi:battery-clock"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Battery Optimizer"

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            coordinator, config_entry, "battery_optimizer_switch", DEVICE_TYPE_INVERTER
        )

    @property
    def is_on(self) -> bool:
        """Return True if optimizer input_boolean is on."""
        state = self.hass.states.get(OPTIMIZER_ENTITY)
        return state is not None and state.state == "on"

    @property
    def available(self) -> bool:
        """Available if the optimizer input_boolean exists.

        Deliberately does NOT chain to `super().available` (i.e. to
        `coordinator.last_update_success`). This is the manual kill switch for the
        AppDaemon battery optimizer, and it is most needed precisely when the Modbus
        link is unhealthy: making it unavailable during a failed poll would hide the
        one control that stops further inverter writes. Turning it off still attempts
        the 30411 clear, which is allowed to fail independently.
        """
        return self.hass.states.get(OPTIMIZER_ENTITY) is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable optimizer: turn on input_boolean (it will sync TOUs on next cycle)."""
        await self.hass.services.async_call(
            "input_boolean", "turn_on",
            {"entity_id": OPTIMIZER_ENTITY},
        )
        _LOGGER.info("[WIT] Battery optimizer enabled")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable optimizer: clear TOUs from inverter + turn off input_boolean."""
        # 1. Clear TOU schedule from inverter
        try:
            success = await self.hass.async_add_executor_job(
                self.coordinator.modbus_client.write_register,
                VPP_TOU_NUM_PERIODS, 0,
            )
            if success:
                _LOGGER.info("[WIT] Cleared TOU schedule (30411=0)")
                setattr(self.coordinator, "wit_vpp_tou_periods", 0)
            else:
                _LOGGER.error("[WIT] Failed to clear TOU schedule")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("[WIT] TOU clear failed: %s", err)

        # 2. Turn off optimizer input_boolean
        await self.hass.services.async_call(
            "input_boolean", "turn_off",
            {"entity_id": OPTIMIZER_ENTITY},
        )
        _LOGGER.info("[WIT] Battery optimizer disabled")
        await self.coordinator.async_request_refresh()
