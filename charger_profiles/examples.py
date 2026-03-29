"""
Example Charger Profiles — vendor/model specific behavior definitions.

These are real-world profiles built from compliance testing and field experience.
They serve as:
  1. Working examples for how to define profiles
  2. Ready-to-use profiles if you happen to run the same hardware

To use: these are loaded automatically by the registry by default.
To override: call `registry.register_profile(your_profile)` before startup,
or set CHARGER_PROFILES_YAML env var to point to a YAML file.
"""
from charger_profiles.registry import ChargerProfile

# ── Maxpower CCS2 V3 (60kW DC) ───────────────────────────────────────────────

MAXPOWER_CCS2_V3 = ChargerProfile(
    id="maxpower-ccs2-v3",
    vendor="MAXPOWER",
    model_pattern=r"CCS2.*",
    firmware_pattern=r"DC2_D_V3\..*",
    description="Maxpower 60kW DC CCS2 dual-gun, firmware V3.x",

    # MeterValues
    sends_power_measurand=True,          # DOES send Power.Active.Import (in W)
    power_unit="W",
    energy_unit="Wh",
    meter_interval_sec=10,
    has_dual_voltage_current=True,        # Sends 2x Voltage + 2x Current per sample
    soc_available=True,

    # Session
    authorize_after_remote_start=True,    # Authorize comes AFTER RemoteStart acceptance
    remote_start_latency_ms=200,
    preparing_on_boot=True,               # Connectors show Preparing after boot, not Available
    reports_connector_zero=True,           # Reports connector 0 (whole charger) status
    resumes_session_after_reboot=False,    # Does NOT resume sessions after reboot
    resumes_session_after_reconnect=True,  # Continues session on WS reconnect

    # Boot
    boot_time_sec=20,
    reconnect_retry_sec=10,
    sends_boot_on_reconnect=False,         # Only Heartbeat on reconnect, Boot only on power cycle
    stop_reason_on_reboot="Other",
    sends_status_on_boot=False,            # COMPLIANCE: intermittent

    # Power
    max_power_kw=60.0,
    ramp_time_sec=30,
    power_tapers_above_soc=80,

    # Protocol compliance (from compliance test 2026-03-29)
    smart_charging_safe=False,             # COMPLIANCE: drops WS connection on SetChargingProfile
    unknown_action_returns_error=False,    # COMPLIANCE: returns CALL_RESULT to unknown actions
    heartbeat_drift_pct=15.0,

    quirks=[
        "Dual V/I in MeterValues: first pair = DC connector output, second = DC bus/input",
        "meterStart in Wh (not kWh)",
        "Connector 0 StatusNotification on boot (Available) — not a real connector",
        "Both connectors go to Preparing on boot even without cables",
        "Power.Active.Import sent in W (not kW) — values like 55826",
        "Authorize sent AFTER accepting RemoteStart (reversed from OCPP spec expectation)",
        "No session resume after reboot — active transaction is lost",
        "20s boot time from power cycle to BootNotification",
        "SHARED DC BUS: when one connector is Finishing, the other is limited to ~30kW (half power)",
        "FIRMWARE BUG: Finishing state locks DC bus — does NOT release after session end. Requires reboot to get full power on other connector.",
        "Full 57kW only available after reboot or when other connector reaches Available naturally",
        # Compliance test findings (2026-03-29):
        "PROTOCOL: returns CALL_RESULT for unknown actions (should be CALL_ERROR per §4)",
        "PROTOCOL: drops WebSocket on SmartCharging commands (SetChargingProfile, ClearChargingProfile, GetCompositeSchedule)",
        "PROTOCOL: intermittent StatusNotification on boot — sometimes sends for all connectors, sometimes doesn't",
        "PROTOCOL: heartbeat drift up to ~10% from configured interval",
    ],
)

# ── Hongjiali chargers ────────────────────────────────────────────────────────

HONGJIALI_120KW = ChargerProfile(
    id="hongjiali-120kw",
    vendor="HONGJIALI",
    model_pattern=r"ENC-DCL120B.*",
    firmware_pattern=r".*",
    description="Hongjiali 120kW DC CCS2 dual-gun (OCPP 2.0.1)",

    # Defaults — will be updated when chargers are profiled
    sends_power_measurand=True,
    meter_interval_sec=15,
    has_dual_voltage_current=False,
    max_power_kw=120.0,
    ramp_time_sec=45,

    quirks=["Profile TBD — based on vendor spec, not field-tested"],
)

HONGJIALI_30KW_PORTABLE = ChargerProfile(
    id="hongjiali-30kw-portable",
    vendor="HONGJIALI",
    model_pattern=r"ENC-DCX030A.*",
    firmware_pattern=r".*",
    description="Hongjiali 30kW portable DC charger",

    sends_power_measurand=True,
    meter_interval_sec=30,
    max_power_kw=30.0,
    ramp_time_sec=10,

    quirks=["Profile TBD — based on vendor spec, not field-tested"],
)

# Ordered list for the registry (specific → generic)
ALL_EXAMPLE_PROFILES = [
    MAXPOWER_CCS2_V3,
    HONGJIALI_120KW,
    HONGJIALI_30KW_PORTABLE,
]
