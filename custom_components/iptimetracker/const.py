from homeassistant.config_entries import ConfigEntry


DOMAIN = "iptimetracker"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TRACKED_MACS = "tracked_macs"
CONF_DEVICE_NICKNAMES = "device_nicknames"
CONF_CONSIDER_HOME = "consider_home"

DEFAULT_HOST = "192.168.0.1"
DEFAULT_USERNAME = "admin"

# Not user-configurable - fixed at the common smart-home-tracker defaults
# (matching Home Assistant's own historical device_tracker component default)
# rather than exposed as options.
SCAN_INTERVAL = 30  # seconds between router polls
RSSI_LIMIT = -90  # dBm; only applied to EasyMesh satellite stations
AVAILABILITY_GRACE = 60  # seconds a coordinator poll failure is tolerated before entities report unavailable

# Default for CONF_CONSIDER_HOME, used until the user picks a preset in
# options (⚙️ > settings) - a device that drops off the client list stays
# "home" for this long before flipping to "away", to avoid presence flapping.
DEFAULT_CONSIDER_HOME = 180
CONSIDER_HOME_PRESETS = (30, 60, 120, 180)  # seconds; offered as the settings dropdown


def entity_unique_id(entry: ConfigEntry, suffix: str) -> str:
    """Build an entity unique ID from the stable config-entry unique ID."""
    scope = entry.unique_id or entry.data[CONF_HOST]
    return f"{scope}_{suffix}"
