"""Constants for the LocalSky integration."""
from __future__ import annotations

DOMAIN = "localsky"

# Config-flow keys.
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USE_HTTPS = "use_https"

# Options-flow keys.
OPT_USE_SSE = "use_sse"
OPT_POLL_INTERVAL = "poll_interval_seconds"
OPT_DEFAULT_RUN_SECONDS = "default_run_seconds"

# Defaults.
DEFAULT_PORT = 8090
DEFAULT_USE_HTTPS = False
DEFAULT_USE_SSE = True
# Polling cadence used as a fallback when SSE is unavailable or
# explicitly disabled in options. LocalSky's snapshots update every ~3s
# (tempest) / ~10s (irrigation); 30s polling is the sweet spot.
DEFAULT_POLL_INTERVAL = 30
DEFAULT_RUN_SECONDS = 600  # 10 min — matches LocalSky dashboard quick-run

# Minimum LocalSky service version the integration is built against.
# Used by /api/v1/info probe at setup time; we warn on lower versions
# but don't refuse.
MIN_SERVICE_VERSION = "0.2.0"
MIN_API_VERSION = "1.0.0"

# Canonical API prefix on the LocalSky instance. The aperturelabs
# internal deployment mounts both /api/* (legacy) and /api/v1/*
# (canonical with /info). New HACS installs target /api/v1.
API_PREFIX = "/api/v1"

# Service-action `kind` values dispatched to /api/v1/irrigation/action.
# These match the tagged-enum `Action` in localsky/src/api/irrigation.rs.
ACTION_RUN = "run"
ACTION_STOP = "stop"
ACTION_STOP_ALL = "stop_all"
ACTION_SET_PAUSE_UNTIL = "set_pause_until"
ACTION_CLEAR_PAUSE_UNTIL = "clear_pause_until"
ACTION_SET_THRESHOLD = "set_threshold"
ACTION_TOGGLE = "toggle"
ACTION_SET_OVERRIDE_TOMORROW = "set_override_tomorrow"
ACTION_RUN_SEQUENCE_NOW = "run_sequence_now"

# Threshold slider keys understood by LocalSky's SetThreshold action.
THRESHOLD_KEYS = ("max_wind_mph", "min_temp_f", "rain_skip_in")

# Per-threshold UI hints — (min, max, step, unit).
THRESHOLD_LIMITS: dict[str, tuple[float, float, float, str | None]] = {
    "max_wind_mph": (0.0, 50.0, 1.0, "mph"),
    "min_temp_f": (20.0, 60.0, 1.0, "°F"),
    "rain_skip_in": (0.0, 1.0, 0.05, "in"),
}

# Permitted slugs for irrigation_override_tomorrow.
OVERRIDE_OPTIONS = ("none", "skip", "run")
