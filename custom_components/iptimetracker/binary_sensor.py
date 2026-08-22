from __future__ import annotations

from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, entity_unique_id
from .coordinator import IptimeDataUpdateCoordinator
from .entity import GracefulAvailabilityMixin


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            IptimeWanLinkBinarySensor(coordinator, entry),
            IptimeRouterConnectivityBinarySensor(coordinator, entry),
        ]
    )


class IptimeWanLinkBinarySensor(
    GracefulAvailabilityMixin,
    CoordinatorEntity[IptimeDataUpdateCoordinator],
    BinarySensorEntity,
):
    """Physical link state of the router's WAN (internet) port.

    This is independent of any single device's Wi-Fi/LAN connection - off
    here means the router itself has lost its uplink, which almost always
    points at the ISP/modem side rather than any one device on the network.
    Unavailable (not off) on firmware this integration can't query WAN link
    state on yet, so it never reports a false "down".

    wan_link comes back None both when a poll fails outright and when the
    WAN-status sub-request specifically errors out (see
    IptimeClient.get_wan_link_status) even though the main poll succeeded -
    either way that's "we don't know yet", not "the link is down", so it
    gets the same grace period as a failed poll instead of flapping
    unavailable on every such hiccup.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "ipTIME 인터넷(WAN) 연결"

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "wan_link")

    @property
    def _is_healthy(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.wan_link is not None
        )

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


class IptimeRouterConnectivityBinarySensor(
    CoordinatorEntity[IptimeDataUpdateCoordinator], BinarySensorEntity
):
    """Whether Home Assistant can currently reach and log into the router.

    Distinct from the WAN sensor above: that one reports the router's own
    uplink to the ISP. This one reports whether polling requests from HA to
    the router (login, client list, EasyMesh topology) are actually
    succeeding - e.g. the router rebooting, or a network hiccup between HA
    and the router, flips this off while every other entity in this
    integration is going unavailable right along with it, which makes those
    look like "the device dropped" rather than "the whole integration lost
    the router". This sensor stays available (device_class connectivity,
    off = trouble) specifically so there's still something to check.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_name = "ipTIME 공유기 통신 상태"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "router_connectivity")
        self._last_success: datetime | None = None

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        if self.coordinator.last_update_success:
            self._last_success = dt_util.utcnow()
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"mesh_enabled": self.coordinator.data.mesh_enabled}
        if self._last_success is not None:
            attrs["last_success"] = self._last_success.isoformat()
        if self.coordinator.last_exception:
            attrs["last_error"] = str(self.coordinator.last_exception)
        return attrs
