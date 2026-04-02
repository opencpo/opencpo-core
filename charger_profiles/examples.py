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
    vendor="hjl",
    model_pattern=r"ENC-DC[XL].*",      # firmware reports ENC-DCX030A, hardware is ENC-DCL120B
    firmware_pattern=r"1\.0\.26\..*",
    description="Hongjiali 120kW DC CCS2 dual-gun — compliance + load tested 2026-04-02",

    # MeterValues (confirmed via load test 2026-04-02)
    sends_power_measurand=False,         # does NOT send Power.Active.Import — only V, A, Wh, SoC
    power_unit="W",
    energy_unit="Wh",                    # Energy.Active.Import.Register in Wh (e.g. "76171.0")
    meter_interval_sec=13,               # measured ~13-14s between samples (config says 15)
    has_dual_voltage_current=False,      # single V + single A per sample
    soc_available=True,                  # sends SoC in Percent

    # Session
    authorize_after_remote_start=False,
    preparing_on_boot=False,             # connectors show Available after boot, Preparing only when cable plugged
    reports_connector_zero=True,         # sends StatusNotification for connector 0
    resumes_session_after_reboot=False,
    resumes_session_after_reconnect=True,

    # Boot
    boot_time_sec=15,
    reconnect_retry_sec=15,
    sends_boot_on_reconnect=False,
    sends_status_on_boot=True,           # sends StatusNotification for all connectors (0,1,2)

    # Power
    max_power_kw=120.0,
    ramp_time_sec=10,
    power_tapers_above_soc=80,

    # Protocol compliance (from compliance test 2026-04-02)
    smart_charging_safe=False,            # tester's SmartCharging format causes disconnect — needs investigation
    unknown_action_returns_error=True,
    heartbeat_drift_pct=15.0,            # drifts after fresh boot, stabilizes after ChangeConfiguration

    quirks=[
        # Factory compliance + load test 2026-04-02
        "IDENTITY: firmware reports vendor='hjl', model='ENC-DCX030A' — actual hardware is ENC-DCL120B 120kW",
        "IDENTITY: charger ID comes from WebSocket URL path, not firmware",
        "BOOT: IMSI present (861963072183558) — 4G modem",
        "BOOT: meterType='0', meterSerialNumber='000000000001' — placeholder values",
        "PROTOCOL: 50 configuration keys, SupportedFeatureProfiles = Core,LocalAuthListManagement,SmartCharging,Reservation,RemoteTrigger",
        "PROTOCOL: correctly returns CallError:NotImplemented for unknown actions",
        "PROTOCOL: handles malformed JSON gracefully",
        "REMOTE START: works correctly via OCPP Core API — Accepted, session starts, MeterValues flow",
        "METER VALUES: 4 measurands — SoC (%), Current.Import (A), Energy.Active.Import.Register (Wh), Voltage (V)",
        "METER VALUES: does NOT send Power.Active.Import — calculate from V×A",
        "METER VALUES: format=Raw, context=Sample.Periodic, interval ~13-14s",
        "METER VALUES: DC bus voltage ~995V under load",
        "HEARTBEAT: drifts after fresh boot, stabilizes to 30s after ChangeConfiguration",
        "SMART CHARGING: tester format causes disconnect — needs investigation",
        "CONNECTORS: 2 connectors, Available on boot, Preparing when cable connected",
        "MONTA COMPATIBLE: confirmed by client — RemoteStart worked flawlessly on Monta",
    ],
)

HONGJIALI_30KW_PORTABLE = ChargerProfile(
    id="hongjiali-30kw-portable",
    vendor="hjl",
    model_pattern=r"ENC-DCX030A.*",
    firmware_pattern=r"1\.0\.26\..*",
    description="Hongjiali 30kW portable DC charger, firmware 1.0.26.x — compliance tested 2026-04-02",

    # MeterValues (confirmed via load test 2026-04-02)
    sends_power_measurand=False,         # does NOT send Power.Active.Import — only V, A, Wh, SoC
    power_unit="W",
    energy_unit="Wh",                    # Energy.Active.Import.Register in Wh (e.g. "76171.0")
    meter_interval_sec=13,               # measured ~13-14s between samples (config says 15)
    has_dual_voltage_current=False,      # single V + single A per sample
    soc_available=True,                  # sends SoC in Percent

    # Session
    authorize_after_remote_start=False,
    preparing_on_boot=False,             # connectors show Available after boot, Preparing only when cable plugged
    reports_connector_zero=True,         # sends StatusNotification for connector 0
    resumes_session_after_reboot=False,
    resumes_session_after_reconnect=True,

    # Boot
    boot_time_sec=15,
    reconnect_retry_sec=15,
    sends_boot_on_reconnect=False,       # only sends Heartbeat on reconnect, Boot on power cycle
    sends_status_on_boot=True,           # sends StatusNotification for all connectors (0,1,2)

    # Power (from load test data)
    max_power_kw=30.0,
    ramp_time_sec=10,                    # ramps from 0 to ~10kW in first sample interval
    power_tapers_above_soc=80,

    # Protocol compliance (from compliance test 2026-04-02)
    smart_charging_safe=False,            # TESTER BUG: tester's SmartCharging format crashes connection.
                                          # RemoteStart works fine via OCPP Core API. Needs investigation.
    unknown_action_returns_error=True,    # correctly returns NotImplemented for unknown actions
    heartbeat_drift_pct=15.0,            # first boot drifts, stabilizes to 30s ±10% after ChangeConfiguration

    quirks=[
        # Compliance test + load test 2026-04-02
        "VENDOR: hjl (lowercase in BootNotification, not HONGJIALI)",
        "BOOT: IMSI present (861963072183558) — 4G modem charger",
        "BOOT: meterType='0', meterSerialNumber='000000000001' — placeholder values",
        "PROTOCOL: 50 configuration keys supported",
        "PROTOCOL: SupportedFeatureProfiles = Core,LocalAuthListManagement,SmartCharging,Reservation,RemoteTrigger",
        "PROTOCOL: correctly returns CallError:NotImplemented for unknown actions (spec-compliant)",
        "PROTOCOL: handles malformed JSON gracefully without disconnecting",
        "PROTOCOL: no concurrent CALL violations",
        "HEARTBEAT: drifts after fresh boot (~4.7s), stabilizes to 30s after ChangeConfiguration",
        "REMOTE START: Accepted — works correctly via OCPP Core API. Tester's format causes disconnect (tester bug).",
        "METER VALUES: 4 measurands — SoC (%), Current.Import (A), Energy.Active.Import.Register (Wh), Voltage (V)",
        "METER VALUES: does NOT send Power.Active.Import — calculate from V×A if needed",
        "METER VALUES: format=Raw, context=Sample.Periodic on all values",
        "METER VALUES: interval ~13-14s (config=15s)",
        "METER VALUES: energy in Wh with decimal (e.g. '76171.0')",
        "METER VALUES: voltage reports high DC voltage (~995V at load) — this is the DC bus voltage",
        "SMART CHARGING: tester's SetChargingProfile causes disconnect — needs investigation if it's payload format or firmware",
        "TRIGGER: TriggerMessage works on second run (first run timed out — timing issue)",
        "LOCAL AUTH: GetLocalListVersion returns -1 (empty list, working)",
        "UNLOCK: UnlockConnector returns NotSupported",
        "RESET: both Soft and Hard reset accepted",
        "CONFIG: ChangeConfiguration accepted for known keys, NotSupported for unknown — spec-compliant",
        "CONNECTORS: 2 connectors, Available on boot, Preparing when cable connected",
        "SESSION: transaction IDs are sequential integers (396, 397, ...)",
        "MONTA: RemoteStart worked flawlessly on Monta — confirmed by client. Our stack works too.",
    ],
)

# Ordered list for the registry (specific → generic)
ALL_EXAMPLE_PROFILES = [
    MAXPOWER_CCS2_V3,
    HONGJIALI_120KW,
    HONGJIALI_30KW_PORTABLE,
]
