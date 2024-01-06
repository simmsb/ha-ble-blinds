from dataclasses import dataclass

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from bleak import BLEDevice, BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
    retry_bluetooth_connection_error,
)
from bluetooth_sensor_state_data import BluetoothData
from home_assistant_bluetooth import BluetoothServiceInfo

from .const import POSITION_UUID, UPDATE_SECONDS

import logging

_LOGGER = logging.getLogger("ble_blinds")

class BLEBlindData(BluetoothData):

    def __init__(self) -> None:
        super().__init__()

    def _start_update(self, service_info: BluetoothServiceInfo):
        manufacturer_data = service_info.manufacturer_data
        address = service_info.address
        self.set_device_type("BLE Blind")
        self.set_device_name(f"BLE Blind {address}")
        self.set_title(f"BLE Blind {address}")
        _LOGGER.debug("got service_info: %s", service_info)

    def poll_needed(
        self, service_info: BluetoothServiceInfo, last_poll: float | None
    ) -> bool:
        """
        This is called every time we get a service_info for a device. It means the
        device is working and online.
        """
        if last_poll is None:
            return True
        update_interval = UPDATE_SECONDS
        return last_poll > update_interval

    @retry_bluetooth_connection_error()
    async def _get_payload(self, client: BleakClientWithServiceCache) -> None:
        position_char = client.services.get_characteristic(POSITION_UUID)
        position = await client.read_gatt_char(position_char)
        self.update_sensor("position", None, str(position), None, "Position")

    async def connect(self):
        try:
            client = await establish_connection(
                BleakClientWithServiceCache, self.device, self.device.address
            )
            await self._get_payload(client)
        except BleakError as err:
            _LOGGER.warning(f"Reading gatt characters failed with err: {err}")
        finally:
            await client.disconnect()
        return self._finish_update()
