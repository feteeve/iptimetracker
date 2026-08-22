DOMAIN = "iptimetracker"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_TRACKED_MACS = "tracked_macs"

DEFAULT_HOST = "192.168.0.1"
DEFAULT_USERNAME = "admin"

# Not user-configurable - fixed at the common smart-home-tracker defaults
# (matching Home Assistant's own historical device_tracker component default)
# rather than exposed as options.
SCAN_INTERVAL = 30  # seconds between router polls
RSSI_LIMIT = -90  # dBm; only applied to EasyMesh satellite stations
CONSIDER_HOME = 180  # seconds a device stays "home" after it drops off the client list
AVAILABILITY_GRACE = 60  # seconds a coordinator poll failure is tolerated before entities report unavailable
