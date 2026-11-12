"""Constants for the PSE&G Energy integration."""

DOMAIN = "pseg"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_COOKIE = "cookie"
CONF_MFA_METHOD = "mfa_method"
CONF_MFA_CODE = "mfa_code"
CONF_AUTH_SERVER_URL = "auth_server_url"
CONF_ELECTRIC_RATE = "electric_rate"
CONF_AUTH_TYPE = "auth_type"
CONF_KEEP_ALIVE = "keep_alive"
CONF_PORTAL_URL = "portal_url"

AUTH_TYPE_MYMETER = "mymeter"
AUTH_TYPE_OKTA = "okta"
AUTH_TYPE_COOKIE = "cookie"

DEFAULT_MFA_METHOD = "sms"
DEFAULT_SCAN_INTERVAL_HOURS = 4
# MyMeter sessions time out after about 10 minutes without requests; with pings
# every 10 minutes they still expired at random, every 4 minutes they stay alive.
# Each delay is picked at random within this range rather than on a fixed beat.
KEEPALIVE_MIN_SECONDS = 4 * 60
KEEPALIVE_MAX_SECONDS = 5 * 60
KEEPALIVE_RETRY_SECONDS = 60
DEFAULT_AUTH_SERVER_URL = "http://127.0.0.1:8000"
# MyMeter portals the integration can sign in to, by base URL. Entries created
# before portal selection have no portal stored and use the default (NJ).
PORTAL_URL_NJ = "https://mysmartenergy.nj.pseg.com"
PORTAL_URL_LI = "https://mysmartenergy.psegliny.com"
DEFAULT_PORTAL_URL = PORTAL_URL_NJ
PORTALS = {
    PORTAL_URL_NJ: "PSE&G New Jersey",
    PORTAL_URL_LI: "PSEG Long Island (experimental)",
}
# MyMeter keeps the selected service (electric or gas) in the session, and the usage
# chart shows whichever one was viewed last, in the browser or by this integration.
# On portals whose accounts can have both, the electric service is selected before
# each fetch. Portals without an entry (Long Island: electric only) are left as they are.
SERVICE_TYPE_ELECTRIC = 1  # gas is 4
PORTAL_SERVICE_TYPES = {
    PORTAL_URL_NJ: SERVICE_TYPE_ELECTRIC,
}
DEFAULT_ELECTRIC_RATE = 0.247

MFA_METHOD_SMS = "sms"
MFA_METHOD_EMAIL = "email"

ATTR_METER = "meter"
ATTR_LATEST_DATE = "latest_date"
ATTR_PREVIOUS_DATE = "previous_date"
ATTR_PREVIOUS_KWH = "previous_kwh"
ATTR_PREVIOUS_COST = "previous_cost"
ATTR_LATEST_COST = "latest_cost"
ATTR_RATE_APPLIED = "rate_applied"
ATTR_RATE_SOURCE = "rate_source"
ATTR_RATE_7D_AVG = "rate_7d_avg"
ATTR_DAYS_USED_FOR_RATE = "days_used_for_rate"
ATTR_IS_EXACT_PORTAL_COST = "is_exact_portal_cost"
ATTR_HISTORY_14D = "history_14d"
ATTR_TEMPERATURE_HIGH = "temperature_high"
ATTR_TEMPERATURE_LOW = "temperature_low"
ATTR_TEMPERATURE_AVG = "temperature_avg"
