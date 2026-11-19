"""Shared settings; related: config_flow.py, controller.py, runtime.py and translations."""

DOMAIN = "adaptive_floor_heating"
NAME = "Adaptive Floor Heating Climate"

CONF_MODE = "mode"
CONF_HEATER = "heater"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_OUTDOOR_TEMPERATURE_SENSOR = "outdoor_temperature_sensor"
CONF_ROOM_COUNT = "room_count"
CONF_ZONES = "zones"
CONF_ZONE_ID = "zone_id"
# Standalone measured-temperature rooms stored in entry options as
# [{name, sensors: [entity_id], occupancy: entity_id | None}]; related: rooms.py, config_flow.py.
# The occupancy dwell below is a separate top-level option applied live like rooms.
CONF_ROOMS = "rooms"
CONF_ROOM_NAME = "name"
CONF_ROOM_SENSORS = "sensors"
CONF_ROOM_OCCUPANCY = "occupancy"
MAX_ROOM_SLOTS = 4
# Blend the measured temperature over 15 minutes when the active-room set changes.
ROOM_TRANSITION_SECONDS = 900
# A room counts as occupied only after its sensor stayed on (and stops after it
# stayed off) this many minutes; brief occupancy flicker never reselects rooms.
CONF_OCCUPANCY_DWELL = "occupancy_dwell_minutes"
DEFAULT_OCCUPANCY_DWELL_MINUTES = 10
MAX_OCCUPANCY_DWELL_MINUTES = 120
# A room-set switch that moves the reference less than this (°C) neither blends
# nor excludes the running cycle from learning.
ROOM_SWITCH_MIN_DELTA = 0.4

MODE_STANDALONE = "standalone"
MODE_MULTI_ZONE = "multi_zone"
MODE_MULTI_ZONE_INTEGRATED = "multi_zone_integrated"
MAX_ROOMS = 32
MIN_INTEGRATED_ROOMS = 2
CONTEXT_HEATERS = "selected_heaters"

DEFAULT_TARGET = 20.0
DEFAULT_HOME_TEMPERATURE = 23.0
DEFAULT_AWAY_TEMPERATURE = 18.0
MIN_TARGET = 18.0
MAX_TARGET = 30.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_HEAT_HOT_TOLERANCE = 0.0
DEFAULT_MIN_ON = 900
# Floor heating keeps releasing heat well after the actuator turns off.
DEFAULT_MIN_OFF = 1800
MIN_CONFIG_MIN_OFF = 1800
DEFAULT_SENSOR_TIMEOUT = 900
# Verify a learned runtime cutoff before committing its final OFF command.
OVERHEAT_TEMPERATURE = 35.0
OVERHEAT_RESET = 34.0
RECOVERY_INTERVAL = 30.0
HEATER_UNAVAILABLE_DELAY = 10.0
COMMAND_TIMEOUT = 10.0
OFF_RETRY_DELAYS = (5.0, 10.0)
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_HEAT_HOT_TOLERANCE = "heat_hot_tolerance"
CONF_MIN_ON = "minimum_on_time"
CONF_MIN_OFF = "minimum_off_time"
CONF_SENSOR_TIMEOUT = "sensor_timeout"
CONF_PEAK_OBSERVATION_HOURS = "peak_observation_hours"
DEFAULT_PEAK_OBSERVATION_HOURS = 6
MIN_PEAK_OBSERVATION_HOURS = 1
MAX_PEAK_OBSERVATION_HOURS = 12
CONF_HOME_TEMPERATURE = "home_temperature"
CONF_AWAY_TEMPERATURE = "away_temperature"
# Daily target slots stored as [{time, temperature, days}]; related: schedule.py, config_flow.py.
CONF_SCHEDULE = "schedule_slots"
# Selectable predictive start strength; related: runtime.py, select.py, storage.py.
PREDICTION_MODES = ("eco", "balanced", "comfort")
# Model labels retained in historical forecasts and accuracy records; related: storage.py.
OFF_MODELS = ("runtime", "additive", "hybrid")
DEFAULT_PREDICTION_MODE = "balanced"
PERSISTED_LEARNING_MODELS = ("existing", "curve")
LATCHED_FAULTS = frozenset(
    {"actuation_fault", "heater_unavailable", "overheat", "external_override"}
)

# Climate-entity actuator (e.g. a boiler room controller with its own thermostat).
# ON raises the controller setpoint far above room temperature so it fires;
# OFF lowers it far below so it idles without cutting power (hot water stays on).
CLIMATE_ON_TEMPERATURE = 30.0
CLIMATE_OFF_TEMPERATURE = 10.0
HEATER_DOMAINS = ("switch", "climate")
# Curve/observation invalid reason when the measured room set changed; related: runtime.py, frontend/panel.js.
ROOM_SET_CHANGED = "ROOM_SET_CHANGED"
