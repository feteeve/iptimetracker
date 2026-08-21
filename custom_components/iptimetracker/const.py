DOMAIN = "iptimetracker"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CONSIDER_HOME = "consider_home"
CONF_RSSI_LIMIT = "rssi_limit"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TRACKED_MACS = "tracked_macs"

DEFAULT_HOST = "192.168.0.1"
DEFAULT_USERNAME = "admin"
DEFAULT_CONSIDER_HOME = 180  # seconds a device stays "home" after it drops off the client list
DEFAULT_RSSI_LIMIT = -90  # dBm; only applied to EasyMesh satellite stations
DEFAULT_SCAN_INTERVAL = 30  # seconds between router polls

MAX_ATTRIBUTE_ITEMS = 100  # protect Recorder from unbounded state attributes
