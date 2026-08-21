DOMAIN = "iptimetracker"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TRACKED_MACS = "tracked_macs"

DEFAULT_HOST = "192.168.0.1"
DEFAULT_USERNAME = "admin"

# Not user-configurable: presence smoothing ("consider_home") is intentionally
# not built in - device_tracker reports the router's raw online/offline state
# and any grace period is left to the user's own automations.
SCAN_INTERVAL = 30  # seconds between router polls
RSSI_LIMIT = -90  # dBm; only applied to EasyMesh satellite stations
