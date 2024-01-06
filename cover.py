"""Blind BLE integration light platform."""
from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    ATTR_POSITION,
    ATTR_TILT_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.helpers import device_registry as dr

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

class BLEBlindEntity(
        CoordinatorEntity,
        CoverEntity):
    """Representation of LEDBLE device."""

    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    def __init__(self, coordinator, device, name):
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = device.address
        self._attr_device_info = DeviceInfo(
            name=name,
            model=f"Blind shutter",
            sw_version="0.1",
            connections={(dr.CONNECTION_BLUETOOTH, device.address)},
        )
        self._attr_device_class = CoverDeviceClass.SHUTTER

    @callback
    def _async_update_attrs(self) -> None:
        """Handle updating _attr values."""
        device = self._device
        self._attr_position = device.position

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        self._device.set_position(100)

    async def async_close_cover(self, **kwargs):
        """Close the cover."""
        self._device.set_position(0)

    @property
    def current_cover_position(self) -> int:
        return self._device.position

    @property
    def is_closed(self) -> int:
        return self.current_cover_position == 0

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        The sensor is only created when the device is seen.

        Since these are sleepy devices which stop broadcasting
        when not in use, we can't rely on the last update time
        so once we have seen the device we always return True.
        """
        return True

    @property
    def assumed_state(self) -> bool:
        """Return True if the device is no longer broadcasting."""
        return not self.available

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
