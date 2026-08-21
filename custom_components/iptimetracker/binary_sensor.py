from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IptimeDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IptimeWanLinkBinarySensor(coordinator, entry)])


class IptimeWanLinkBinarySensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], BinarySensorEntity
):
    """Physical link state of the router's WAN (internet) port.

    This is independent of any single device's Wi-Fi/LAN connection - off
    here means the router itself has lost its uplink, which almost always
    points at the ISP/modem side rather than any one device on the network.
    Unavailable (not off) on firmware this integration can't query WAN link
    state on yet, so it never reports a false "down".
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "ipTIME 인터넷(WAN) 연결"

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_wan_link"

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.wan_link is not None

    @property
    def is_on(self) -> bool | None:
        wan = self.coordinator.data.wan_link
        return wan.connected if wan else None

    @property
    def extra_state_attributes(self) -> dict:
        wan = self.coordinator.data.wan_link
        if not wan:
            return {}
        attrs: dict = {}
        if wan.speed_mbps is not None:
            attrs["link_speed_mbps"] = wan.speed_mbps
        if wan.duplex:
            attrs["duplex"] = wan.duplex
        if wan.raw:
            attrs["raw"] = wan.raw
        return attrs
