"""
Charger Profile Registry — matches vendor/model/firmware to behavior profiles.

On BootNotification, call `resolve_profile(vendor, model, firmware)` to get
the correct profile. The profile describes what the charger does and doesn't do,
so the handler can adapt without if/else spaghetti.

## Extending

Add profiles at runtime:
    from charger_profiles.registry import register_profile
    register_profile(MyCustomProfile)

Load from YAML:
    from charger_profiles.registry import load_profiles_from_yaml
    load_profiles_from_yaml("/etc/ocpp/profiles.yaml")

Or set env var CHARGER_PROFILES_YAML to auto-load on startup.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChargerProfile:
    """Describes charger-specific behavior for the OCPP handler."""

    # Identity
    id: str                          # e.g. "my-charger-v2"
    vendor: str                      # e.g. "MYVENDOR"
    model_pattern: str               # regex, e.g. "CCS2.*"
    firmware_pattern: str = ".*"     # regex, e.g. "V3\\..*"
    description: str = ""

    # ── MeterValues behavior ─────────────────────────────────────────────
    sends_power_measurand: bool = True          # sends Power.Active.Import?
    power_unit: str = "W"                       # W or kW
    energy_unit: str = "Wh"                     # Wh or kWh
    meter_interval_sec: int = 10                # expected sample interval
    has_dual_voltage_current: bool = False       # sends two V/I pairs per sample?
    soc_available: bool = True                   # sends SoC?

    # ── Session behavior ─────────────────────────────────────────────────
    authorize_after_remote_start: bool = False   # sends Authorize AFTER accepting RemoteStart?
    remote_start_latency_ms: int = 200           # typical RemoteStart response time
    preparing_on_boot: bool = False              # connectors show Preparing after boot (not Available)?
    reports_connector_zero: bool = False          # sends StatusNotification for connector 0?
    resumes_session_after_reboot: bool = False    # keeps session alive across reboots?
    resumes_session_after_reconnect: bool = True  # continues session on WS reconnect (no reboot)?

    # ── Boot/reconnect behavior ──────────────────────────────────────────
    boot_time_sec: int = 20                      # time from power-on to BootNotification
    reconnect_retry_sec: int = 10                # WS reconnect interval
    sends_boot_on_reconnect: bool = False         # sends BootNotification on WS reconnect?
    stop_reason_on_reboot: str = "Other"          # StopTransaction reason when rebooting mid-session
    sends_status_on_boot: bool = True             # sends StatusNotification for all connectors after boot?

    # ── Power characteristics ────────────────────────────────────────────
    max_power_kw: float = 60.0                   # rated max power
    ramp_time_sec: int = 30                      # 0 to max power ramp time
    power_tapers_above_soc: int = 80             # SoC% where power starts tapering

    # ── Protocol compliance (from compliance testing) ────────────────────
    smart_charging_safe: bool = True              # can receive SetChargingProfile without disconnecting?
    unknown_action_returns_error: bool = True     # responds CALL_ERROR to unknown actions (spec-compliant)?
    heartbeat_drift_pct: float = 10.0             # acceptable heartbeat interval drift %

    # ── Quirks (free-form, for documentation) ────────────────────────────
    quirks: list[str] = field(default_factory=list)

    def matches(self, vendor: str, model: str, firmware: str) -> bool:
        """Check if this profile matches the given charger identity."""
        if self.vendor != "*" and self.vendor.upper() != vendor.upper():
            return False
        if not re.match(self.model_pattern, model, re.IGNORECASE):
            return False
        if not re.match(self.firmware_pattern, firmware, re.IGNORECASE):
            return False
        return True


# ── Generic fallback (always last in the registry) ───────────────────────────

GENERIC_OCPP16 = ChargerProfile(
    id="generic-ocpp16",
    vendor="*",
    model_pattern=r".*",
    firmware_pattern=r".*",
    description="Generic OCPP 1.6j charger — conservative defaults",

    sends_power_measurand=False,          # Assume worst case
    has_dual_voltage_current=False,
    preparing_on_boot=False,
    reports_connector_zero=False,
    resumes_session_after_reboot=False,
    max_power_kw=22.0,                    # Assume AC charger

    sends_status_on_boot=True,             # Assume compliant
    smart_charging_safe=True,              # Assume compliant
    unknown_action_returns_error=True,     # Assume compliant

    quirks=["Generic profile — update when charger behavior is known"],
)


# ── Profile Registry ─────────────────────────────────────────────────────────

# Loaded lazily — starts with example profiles, GENERIC_OCPP16 always last.
# Override by calling register_profile() or load_profiles_from_yaml() at startup.
_PROFILES: list[ChargerProfile] = []
_profiles_initialized = False


def _ensure_initialized() -> None:
    """Lazy initialization — load example profiles on first access."""
    global _profiles_initialized
    if _profiles_initialized:
        return
    _profiles_initialized = True

    # Load built-in examples unless disabled
    if os.environ.get("CHARGER_PROFILES_NO_EXAMPLES", "").lower() not in ("1", "true", "yes"):
        try:
            from charger_profiles.examples import ALL_EXAMPLE_PROFILES
            _PROFILES.extend(ALL_EXAMPLE_PROFILES)
            logger.debug(f"Loaded {len(ALL_EXAMPLE_PROFILES)} example charger profiles")
        except ImportError:
            logger.debug("charger_profiles.examples not found — starting with generic only")

    # Always append the generic fallback last
    _PROFILES.append(GENERIC_OCPP16)

    # Auto-load YAML if configured
    yaml_path = os.environ.get("CHARGER_PROFILES_YAML", "")
    if yaml_path:
        try:
            load_profiles_from_yaml(yaml_path)
        except Exception as e:
            logger.warning(f"Failed to load charger profiles from {yaml_path}: {e}")


def register_profile(profile: ChargerProfile, *, prepend: bool = True) -> None:
    """
    Register a custom charger profile at runtime.

    By default, inserts before the generic fallback so specific profiles
    are matched first. Pass prepend=False to append after existing profiles
    (but still before GENERIC_OCPP16).

    Example:
        from charger_profiles.registry import register_profile, ChargerProfile

        MY_CHARGER = ChargerProfile(
            id="my-charger-v1",
            vendor="MYVENDOR",
            model_pattern=r"XDC.*",
            max_power_kw=50.0,
        )
        register_profile(MY_CHARGER)
    """
    _ensure_initialized()

    # Remove GENERIC_OCPP16 temporarily, insert new profile, re-append generic
    non_generic = [p for p in _PROFILES if p.id != "generic-ocpp16"]
    generic = [p for p in _PROFILES if p.id == "generic-ocpp16"]

    if prepend:
        non_generic.insert(0, profile)
    else:
        non_generic.append(profile)

    _PROFILES.clear()
    _PROFILES.extend(non_generic)
    _PROFILES.extend(generic)

    logger.info(f"Registered charger profile: {profile.id}")


def load_profiles_from_yaml(path: str) -> int:
    """
    Load charger profiles from a YAML file. Returns number of profiles loaded.

    YAML format:
        profiles:
          - id: my-charger-v1
            vendor: MYVENDOR
            model_pattern: "XDC.*"
            description: "My 50kW charger"
            max_power_kw: 50.0
            sends_power_measurand: true
            quirks:
              - "Sends energy in kWh, not Wh"

    All fields from ChargerProfile are supported. Only id, vendor, and
    model_pattern are required; everything else uses sensible defaults.

    Example:
        from charger_profiles.registry import load_profiles_from_yaml
        load_profiles_from_yaml("/etc/ocpp/my-profiles.yaml")
    """
    try:
        import yaml
    except ImportError:
        raise ImportError(
            "PyYAML is required for YAML profile loading. "
            "Install it with: pip install pyyaml"
        )

    with open(path) as f:
        data = yaml.safe_load(f)

    profiles_data = data.get("profiles", [])
    count = 0
    for p in profiles_data:
        try:
            profile = ChargerProfile(**p)
            register_profile(profile, prepend=False)
            count += 1
        except Exception as e:
            logger.warning(f"Skipping invalid profile in {path}: {e} — data: {p}")

    logger.info(f"Loaded {count} charger profiles from {path}")
    return count


def resolve_profile(vendor: str, model: str, firmware: str) -> ChargerProfile:
    """Find the best matching profile for a charger."""
    _ensure_initialized()
    for profile in _PROFILES:
        if profile.matches(vendor, model, firmware):
            logger.info(f"Charger profile matched: {profile.id} for {vendor}/{model}/{firmware}")
            return profile

    logger.warning(f"No profile match for {vendor}/{model}/{firmware} — using generic")
    return GENERIC_OCPP16


def get_profile(profile_id: str) -> Optional[ChargerProfile]:
    """Look up a profile by ID."""
    _ensure_initialized()
    for profile in _PROFILES:
        if profile.id == profile_id:
            return profile
    return None


def list_profiles() -> list[ChargerProfile]:
    """Return all registered profiles."""
    _ensure_initialized()
    return list(_PROFILES)
