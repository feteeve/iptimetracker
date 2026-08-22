from __future__ import annotations

import logging
import re
from datetime import datetime

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_NICKNAMES,
    CONF_TRACKED_MACS,
    CONSIDER_HOME,
    DOMAIN,
    entity_unique_id,
)
from .coordinator import (
    DhcpLease,
    IptimeDataUpdateCoordinator,
    StaticLease,
    WirelessClient,
)
from .entity import GracefulAvailabilityMixin

_LOGGER = logging.getLogger(__name__)
_MAC_UNIQUE_ID_PATTERN = re.compile(
    r"(?:^|_)([0-9A-Fa-f]{2}(?:[:_-][0-9A-Fa-f]{2}){5})$"
)


def _unique_id(entry: ConfigEntry, mac: str) -> str:
    return entity_unique_id(entry, mac.replace(":", "_"))


def _mac_from_unique_id(unique_id: str) -> str | None:
    match = _MAC_UNIQUE_ID_PATTERN.search(unique_id)
    if not match:
        return None
    return match.group(1).upper().replace("_", ":").replace("-", ":")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # The device picker (options flow) is the sole source of truth for what
    # gets tracked. Nothing is added automatically just because the router
    # reports it as connected, and nothing is auto-pruned by age either -
    # unchecking a device in the picker is what removes its entity, so a
    # deliberately-picked device (e.g. a named static-IP reservation that's
    # normally offline) never gets treated as an abandoned/stale one.
    allowed_macs: set[str] = {
        mac.upper() for mac in entry.options.get(CONF_TRACKED_MACS, [])
    }
    nicknames: dict[str, str] = {
        mac.upper(): name
        for mac, name in entry.options.get(CONF_DEVICE_NICKNAMES, {}).items()
        if name
    }

    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if registry_entry.domain != "device_tracker" or registry_entry.platform != DOMAIN:
            continue
        mac = _mac_from_unique_id(registry_entry.unique_id)
        if not mac:
            continue
        if mac not in allowed_macs:
            registry.async_remove(registry_entry.entity_id)
            continue
        expected_unique_id = _unique_id(entry, mac)
        if registry_entry.unique_id != expected_unique_id:
            registry.async_update_entity(
                registry_entry.entity_id, new_unique_id=expected_unique_id
            )
        # Older releases wrote the integration nickname into the registry's
        # user-controlled name override. Clear only an unchanged generated
        # value; a real user rename differs from original_name and is kept.
        if (
            registry_entry.name is not None
            and registry_entry.name == registry_entry.original_name
        ):
            registry.async_update_entity(registry_entry.entity_id, name=None)

    async_add_entities(
        [
            IptimeDeviceTracker(coordinator, entry, mac, nicknames.get(mac))
            for mac in sorted(allowed_macs)
        ]
    )


class IptimeDeviceTracker(
    GracefulAvailabilityMixin,
    CoordinatorEntity[IptimeDataUpdateCoordinator],
    ScannerEntity,
    RestoreEntity,
):
    """Track a device's presence on the ipTIME router.

    A device that briefly drops off the client list (Wi-Fi power save, a
    weak-signal hiccup) is kept "home" for CONSIDER_HOME seconds instead of
    flipping to "away" immediately, to avoid presence flapping - matching
    Home Assistant's own historical device_tracker default (180s).
    """

    _attr_source_type = SourceType.ROUTER

    def __init__(
        self,
        coordinator: IptimeDataUpdateCoordinator,
        entry: ConfigEntry,
        mac: str,
        nickname: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._nickname = nickname
        self._attr_unique_id = _unique_id(entry, mac)
        self._last_seen: datetime | None = None
        self._last_known_name: str | None = None

    @property
    def unique_id(self) -> str:
        """Scope a client to this router instead of using the MAC alone."""
        return self._attr_unique_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        previous_state = await self.async_get_last_state()
        if previous_state is None:
            return
        last_seen = previous_state.attributes.get("last_seen")
        if isinstance(last_seen, str):
            self._last_seen = dt_util.parse_datetime(last_seen)
        device_name = previous_state.attributes.get("device_name")
        if isinstance(device_name, str) and device_name and device_name != self._mac:
            self._last_known_name = device_name

    @property
    def _client(self) -> WirelessClient | None:
        for c in self.coordinator.data.connected_clients:
            if c.mac == self._mac:
                return c
        return None

    @property
    def _dhcp_lease(self) -> DhcpLease | None:
        return next(
            (
                lease
                for lease in self.coordinator.data.dhcp_leases
                if lease.mac == self._mac
            ),
            None,
        )

    @property
    def _static_lease(self) -> StaticLease | None:
        return next(
            (
                lease
                for lease in self.coordinator.data.static_leases
                if lease.mac == self._mac
            ),
            None,
        )

    @property
    def _is_healthy(self) -> bool:
        # A failed poll would otherwise bypass the CONSIDER_HOME grace below
        # entirely - see GracefulAvailabilityMixin.
        if not self.coordinator.last_update_success:
            return False
        data = self.coordinator.data
        if data.mesh_topology_available:
            return True
        # The main router list remains authoritative for directly connected
        # and absent non-mesh clients. Only a cached mesh client is unknown.
        return not any(client.mac == self._mac for client in data.mesh_clients)

    @property
    def is_connected(self) -> bool:
        if self._client is not None:
            self._last_seen = dt_util.utcnow()
            return True
        if self._last_seen is None:
            return False
        return (dt_util.utcnow() - self._last_seen).total_seconds() < CONSIDER_HOME

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def hostname(self) -> str | None:
        c = self._client
        # Keep the device-reported/DHCP hostname separate from the
        # administrator-assigned static reservation name.
        if c and c.hostname and c.hostname != self._mac:
            self._last_known_name = c.hostname
            return c.hostname
        lease = self._dhcp_lease
        if lease and lease.hostname and lease.hostname != self._mac:
            self._last_known_name = lease.hostname
            return lease.hostname
        # The device isn't in this poll's client list (offline, or just
        # outside CONSIDER_HOME) - dhcp_leases/static_leases aren't wired up
        # yet on this firmware, so without this cache the name would revert
        # to the bare MAC the instant a device drops off, even while
        # is_connected still reports it "home".
        return self._last_known_name or self._mac

    @property
    def reservation_name(self) -> str | None:
        c = self._client
        if c and c.reservation_name and c.reservation_name != self._mac:
            return c.reservation_name
        lease = self._static_lease
        if lease and lease.hostname and lease.hostname != self._mac:
            return lease.hostname
        return None

    @property
    def device_name_source(self) -> str | None:
        c = self._client
        if c and c.hostname and c.hostname != self._mac:
            return c.hostname_source or "connected_client"
        lease = self._dhcp_lease
        if lease and lease.hostname and lease.hostname != self._mac:
            return lease.name_source
        return None

    @property
    def reservation_name_source(self) -> tuple[str | None, str | None]:
        c = self._client
        if c and c.reservation_name and c.reservation_name != self._mac:
            return c.reservation_name_source, c.reservation_name_confidence
        lease = self._static_lease
        if lease and lease.hostname and lease.hostname != self._mac:
            return lease.name_source, lease.name_confidence
        return None, None

    @property
    def name(self) -> str:
        return self._nickname or self.reservation_name or self.hostname or self._mac

    @property
    def ip_address(self) -> str | None:
        c = self._client
        if c and c.ip:
            return c.ip
        lease = self._dhcp_lease
        if lease:
            return lease.ip
        return None

    @property
    def extra_state_attributes(self) -> dict:
        attrs: dict = {"mac": self._mac}
        c = self._client
        if c:
            attrs["interface"] = c.interface
            if c.rssi is not None:
                attrs["rssi_dbm"] = c.rssi
        if self._last_seen is not None:
            attrs["last_seen"] = self._last_seen.isoformat()

        device_name = self.hostname
        reservation_name = self.reservation_name
        if device_name and device_name != self._mac:
            attrs["device_name"] = device_name
            attrs["device_name_source"] = self.device_name_source
        if reservation_name:
            attrs["reservation_name"] = reservation_name
            reservation_source, reservation_confidence = self.reservation_name_source
            attrs["reservation_name_source"] = reservation_source
            attrs["reservation_name_confidence"] = reservation_confidence
        if self._nickname:
            attrs["nickname"] = self._nickname
            attrs["name_source"] = "nickname"
        elif reservation_name:
            attrs["name_source"] = "static_reservation"
        elif device_name and device_name != self._mac:
            attrs["name_source"] = "device_or_dhcp"
        else:
            attrs["name_source"] = "mac"

        # 고정IP 여부
        static = self._static_lease
        if static:
            attrs["static_ip"] = static.ip

        # DHCP 만료 시간
        lease = self._dhcp_lease
        if lease and lease.expires:
            attrs["dhcp_expires"] = lease.expires

        return attrs
