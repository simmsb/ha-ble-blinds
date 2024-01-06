from dataclasses import dataclass
import asyncio
import struct
from typing import Callable
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic, BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakError,
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)
from home_assistant_bluetooth import BluetoothServiceInfo

from .const import POSITION_READ_UUID, UPDATE_SECONDS, SERVICE_UUID, POSITION_WRITE_UUID

import logging

class CharacteristicMissingError(Exception):
    """Raised when a characteristic is missing."""

DISCONNECT_DELAY = 120
BLEAK_BACKOFF_TIME = 0.25
RETRY_BACKOFF_EXCEPTIONS = (BleakDBusError,)
DEFAULT_ATTEMPTS = 3

_LOGGER = logging.getLogger("ble_blinds")

class BLEBlind:
    def __init__(self, ble_device: BLEDevice, advertisement_data: AdvertisementData | None = None) -> None:
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data
        self._operation_lock = asyncio.Lock()
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._position_read_char: BleakGATTCharacteristic | None = None
        self._position_write_char: BleakGATTCharacteristic | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._client: BleakClientWithServiceCache | None = None
        self._expected_disconnect = False
        self.loop = asyncio.get_running_loop()
        self._callbacks: list[Callable[[int], None]] = []
        self._position = 0

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        """Set the ble device."""
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    @property
    def position(self) -> int:
        return self._position

    @property
    def name(self) -> str:
        """Get the name of the device."""
        return self._ble_device.name or self._ble_device.address

    @property
    def address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def _address(self) -> str:
        """Return the address."""
        return self._ble_device.address

    @property
    def rssi(self) -> int | None:
        """Get the rssi of the device."""
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    def _fire_callbacks(self) -> None:
        """Fire the callbacks."""
        for callback in self._callbacks:
            callback(self._position)

    def register_callback(
        self, callback: Callable[[int], None]
    ) -> Callable[[], None]:
        """Register a callback to be called when the state changes."""

        def unregister_callback() -> None:
            self._callbacks.remove(callback)

        self._callbacks.append(callback)
        return unregister_callback

    async def stop(self) -> None:
        _LOGGER.debug("%s: Stop", self.name)
        await self._execute_disconnect()

    def _notification_handler(self, _sender: int, data: bytearray) -> None:
        """Handle notification responses."""
        try:
            self._position, = struct.unpack("<H", data)
        except Exception as e:
            _LOGGER.exception("Failed to decode position: %s", data)

        self._fire_callbacks()

    async def _execute_position_locked(self, position: int) -> None:
        """Execute command and read response."""
        assert self._client is not None  # nosec
        if not self._position_write_char:
            raise CharacteristicMissingError("write characteristic missing")
        _LOGGER.debug("Writing position: %s", position)
        await self._client.write_gatt_char(self._position_write_char, struct.pack("<H", position), True)

    async def _read_position_locked(self) -> None:
        assert self._client is not None  # nosec
        if not self._position_read_char:
            raise CharacteristicMissingError("Read characteristic missing")
        r = await self._client.read_gatt_char(self._position_read_char)
        self._notification_handler(0, r)

    async def _ensure_connected(self) -> None:
        """Ensure connection to device is established."""
        if self._connect_lock.locked():
            _LOGGER.debug(
                "%s: Connection already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return
        async with self._connect_lock:
            # Check again while holding the lock
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return
            _LOGGER.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self.name,
                self._disconnected,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,
            )
            _LOGGER.debug("%s: Connected; RSSI: %s", self.name, self.rssi)
            resolved = self._resolve_characteristics(client.services)
            if not resolved:
                # Try to handle services failing to load
                resolved = self._resolve_characteristics(await client.get_services())

            self._client = client
            self._reset_disconnect_timer()

            _LOGGER.debug(
                "%s: Subscribe to notifications; RSSI: %s", self.name, self.rssi
            )
            # await client.start_notify(self._position_char, self._notification_handler)

    def _reset_disconnect_timer(self) -> None:
        """Reset disconnect timer."""
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        self._disconnect_timer = self.loop.call_later(
            DISCONNECT_DELAY, self._disconnect
        )

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        """Disconnected callback."""
        if self._expected_disconnect:
            _LOGGER.debug(
                "%s: Disconnected from device; RSSI: %s", self.name, self.rssi
            )
            return
        _LOGGER.warning(
            "%s: Device unexpectedly disconnected; RSSI: %s",
            self.name,
            self.rssi,
        )

    def _disconnect(self) -> None:
        """Disconnect from device."""
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        """Execute timed disconnection."""
        _LOGGER.debug(
            "%s: Disconnecting after timeout of %s",
            self.name,
            DISCONNECT_DELAY,
        )
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        """Execute disconnection."""
        async with self._connect_lock:
            position_read_char = self._position_read_char
            client = self._client
            self._expected_disconnect = True
            self._client = None
            self._position_read_char = None
            self._position_write_char = None
            if client and client.is_connected:
                if position_read_char:
                    try:
                        await client.stop_notify(position_read_char)
                    except BleakError:
                        _LOGGER.debug(
                            "%s: Failed to stop notifications", self.name, exc_info=True
                        )
                await client.disconnect()

    async def _get_position(
        self, retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        await self._ensure_connected()
        await self._get_position_while_connected( retry)

    async def _set_position(
        self, position: int, retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        await self._ensure_connected()
        await self._set_position_while_connected(position, retry)

    async def _get_position_while_connected(
        self, retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        _LOGGER.debug(
            "%s: getting position",
            self.name,
        )
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._get_position_locked()
                return
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                _LOGGER.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    async def _set_position_while_connected(
        self, position: int, retry: int | None = None
    ) -> None:
        """Send command to device and read response."""
        _LOGGER.debug(
            "%s: setting position %s",
            self.name,
            position,
        )
        if self._operation_lock.locked():
            _LOGGER.debug(
                "%s: Operation already in progress, waiting for it to complete; RSSI: %s",
                self.name,
                self.rssi,
            )
        async with self._operation_lock:
            try:
                await self._set_position_locked(position)
                return
            except BleakNotFoundError:
                _LOGGER.error(
                    "%s: device not found, no longer in range, or poor RSSI: %s",
                    self.name,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except CharacteristicMissingError as ex:
                _LOGGER.debug(
                    "%s: characteristic missing: %s; RSSI: %s",
                    self.name,
                    ex,
                    self.rssi,
                    exc_info=True,
                )
                raise
            except BLEAK_EXCEPTIONS:
                _LOGGER.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _set_position_locked(self, position: int) -> None:
        """Send command to device and read response."""
        try:
            await self._execute_position_locked(position)
        except BleakDBusError as ex:
            # Disconnect so we can reset state and try again
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnecting due to error: %s",
                self.name,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            # Disconnect so we can reset state and try again
            _LOGGER.debug(
                "%s: RSSI: %s; Disconnecting due to error: %s", self.name, self.rssi, ex
            )
            await self._execute_disconnect()
            raise

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _get_position_locked(self) -> None:
        """Send command to device and read response."""
        try:
            await self._read_position_locked()
        except BleakDBusError as ex:
            # Disconnect so we can reset state and try again
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            _LOGGER.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnecting due to error: %s",
                self.name,
                self.rssi,
                BLEAK_BACKOFF_TIME,
                ex,
            )
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            # Disconnect so we can reset state and try again
            _LOGGER.debug(
                "%s: RSSI: %s; Disconnecting due to error: %s", self.name, self.rssi, ex
            )
            await self._execute_disconnect()
            raise


    async def set_position(self, position: int):
        await self._set_position(position)
        self._fire_callbacks()

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        """Resolve characteristics."""
        if service := next((s for s in services if s.uuid == SERVICE_UUID), None):
            if char := service.get_characteristic(POSITION_READ_UUID):
                self._position_read_char = char
            if char := service.get_characteristic(POSITION_WRITE_UUID):
                self._position_write_char = char
        return bool(self._position_read_char and self._position_write_char)

    async def update(self):
        await self._ensure_connected()
        await self._get_position()

@dataclass
class BLEBlindData:
    title: str
    device: BLEBlind
    coordinator: DataUpdateCoordinator
