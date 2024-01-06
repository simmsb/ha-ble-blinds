from dataclasses import dataclass

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator


@dataclass
class BLEBlindData:
    title: str
    device: any
    coordinator: DataUpdateCoordinator
