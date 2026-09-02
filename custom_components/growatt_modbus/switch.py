"""Switch platform for Growatt Modbus Integration."""
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.entity import EntityCategory

from .const import (
    CONF_REGISTER_MAP,
    REGISTER_MAPS,
    DEVICE_TYPE_GRID,
    DEVICE_TYPE_INVERTER,
)
from .coordinator import GrowattModbusCoordinator
from .entity import GrowattEntity

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

    register_map_name = config_entry.data.get(CONF_REGISTER_MAP)
    register_map = REGISTER_MAPS.get(register_map_name, {})
    holding_registers = register_map.get('holding_registers', {})

    entities: list[SwitchEntity] = []
    is_wit = str(register_map_name).upper() == "WIT_4000_15000TL3"

    if is_wit:
        # Grid Export switch (register 30201)
        if VPP_EXPORT_LIMIT_POWER_RATE in holding_registers:
            entities.append(GrowattWitExportSwitch(coordinator, config_entry))

        # Battery Optimizer switch (controls input_boolean + TOU clear)
        if VPP_TOU_NUM_PERIODS in holding_registers:
            entities.append(GrowattWitOptimizerSwitch(coordinator, config_entry))

    if entities:
        entry_name = config_entry.data.get("name", config_entry.title)
        _LOGGER.info("Created %d WIT switch entities for %s", len(entities), entry_name)
        async_add_entities(entities)


class GrowattWitExportSwitch(GrowattEntity, SwitchEntity):
    """Toggle grid export on/off (register 30201: 0=off, 100=on)."""

    _attr_icon = "mdi:transmission-tower-export"
    _attr_entity_category = EntityCategory.CONFIG
    # has_entity_name is inherited from GrowattEntity, so HA prefixes the device name.
    # The unique_id suffix must stay `grid_export_switch` - switch.growatt_grid_export
    # is an existing registry row.
    _attr_name = "Grid Export"

    def __init__(
        self,
        coordinator: GrowattModbusCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(
            coordinator, config_entry, "grid_export_switch", DEVICE_TYPE_GRID
        )

    @property
    def is_on(self) -> bool:
        """Return True if export is enabled (30201 > 0)."""
        data = self.coordinator.data
        if data is None:
            return False
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
