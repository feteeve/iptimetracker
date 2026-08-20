DOMAIN = "iptimetracker"

CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CONSIDER_HOME = "consider_home"
CONF_RSSI_LIMIT = "rssi_limit"

DEFAULT_HOST = "192.168.0.1"
DEFAULT_USERNAME = "admin"
DEFAULT_CONSIDER_HOME = 180  # seconds a device stays "home" after it drops off the client list
DEFAULT_RSSI_LIMIT = -90  # dBm; only applied to EasyMesh satellite stations

SCAN_INTERVAL = 30  # seconds
