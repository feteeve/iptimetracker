from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, entity_unique_id
from .coordinator import IptimeDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IptimeDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([IptimeCollectInformationButton(coordinator, entry)])


class IptimeCollectInformationButton(
    CoordinatorEntity[IptimeDataUpdateCoordinator], ButtonEntity
):
    """Run the extended read-only router collection once."""

    _attr_name = "ipTIME 전체 정보 수집"
    _attr_icon = "mdi:database-refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IptimeDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = entity_unique_id(entry, "collect_information")

    async def async_press(self) -> None:
        await self.coordinator.async_collect_diagnostics()
