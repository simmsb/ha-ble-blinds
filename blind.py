"""Blind BLE integration light platform."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN
from .model import BLEBlindData


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light platform for LEDBLE."""
    data: BLEBlindData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([BLEBlindEntity(data.coordinator, data.device, entry.title)])


class BLEBlindEntity(CoordinatorEntity, NumberEntity):
    """Representation of LEDBLE device."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_mode = "slider"

    def __init__(
        self, coordinator: DataUpdateCoordinator, device: Any, name: str
    ) -> None:
        """Initialize an ledble light."""
        super().__init__(coordinator)
        self._device = device
        self._attr_native_value = 0
        self._attr_unique_id = device.address
        self._attr_device_info = DeviceInfo(
            name=name,
            model=f"{device.model_data.description} {hex(device.model_num)}",
            sw_version=hex(device.version_num),
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )
        self._async_update_attrs()

    async def async_set_native_value(self, value: float) -> None:
        pass

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""

    @callback
    def _handle_coordinator_update(self, *args: Any) -> None:
        """Handle data update."""
        self._async_update_attrs()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""
        self.async_on_remove(
            self._device.register_callback(self._handle_coordinator_update)
        )
        return await super().async_added_to_hass()
