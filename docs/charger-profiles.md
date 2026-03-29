# Charger Profiles

The profile system allows ocpp-core to adapt its behavior per charger model without if/else spaghetti in the handler code. On BootNotification, the charger's vendor/model/firmware is matched against registered profiles, and the handler uses the profile to decide how to handle edge cases.

## Why Profiles Exist

Real-world OCPP chargers don't all follow the spec exactly. Common deviations:
- Some don't send `Power.Active.Import` in MeterValues
- Some report energy in kWh instead of Wh
- Some don't send `StatusNotification` after boot unless you ask via `TriggerMessage`
- Some send `StopTransaction` with `reason=Other` when they reconnect (not when the session ends)
- Some need a 90-second ramp at cold temperatures instead of 30

Profiles document and accommodate these deviations in one place.

## Profile Fields

```python
@dataclass(frozen=True)
class ChargerProfile:
    # Identity matching
    id: str                          # Unique profile ID, e.g. "my-charger-v2"
    vendor: str                      # "MYVENDOR" or "*" to match any
    model_pattern: str               # Regex, e.g. r"CCS2.*"
    firmware_pattern: str = ".*"     # Regex for firmware version

    # MeterValues behavior
    sends_power_measurand: bool      # Does it send Power.Active.Import?
    power_unit: str                  # "W" or "kW"
    energy_unit: str                 # "Wh" or "kWh"
    meter_interval_sec: int          # Expected sample interval
    soc_available: bool              # Does it send SoC?

    # Session behavior
    authorize_after_remote_start: bool  # Sends Authorize AFTER RemoteStart?
    preparing_on_boot: bool             # Connectors show Preparing after boot?
    sends_status_on_boot: bool          # Sends StatusNotification after boot?

    # Power characteristics
    max_power_kw: float
    ramp_time_sec: int               # 0→max power ramp duration
    power_tapers_above_soc: int      # SoC% where power starts tapering

    # Protocol compliance
    smart_charging_safe: bool        # Can receive SetChargingProfile safely?
    unknown_action_returns_error: bool  # Responds CALL_ERROR to unknown actions?

    # Free-form quirk documentation
    quirks: list[str]
```

## Profile Matching

On BootNotification, `resolve_profile(vendor, model, firmware)` iterates registered profiles in order and returns the first match. The generic fallback always matches if nothing else does.

```python
from charger_profiles.registry import resolve_profile

profile = resolve_profile("ACME", "FastCharge-120", "FW2.3.1")
# → Returns the ACME FastCharge profile if registered, else GENERIC_OCPP16
```

Matching is:
1. `vendor` compared case-insensitively (or `"*"` matches any)
2. `model_pattern` matched as regex (case-insensitive)
3. `firmware_pattern` matched as regex (case-insensitive)

## The MAXPOWER Quirk: A Real-World Example

The built-in examples include a profile for a DC fast charger with these documented quirks:

```python
MAXPOWER_PROFILE = ChargerProfile(
    id="maxpower-dcl120",
    vendor="MAXPOWER",
    model_pattern=r"DCL120.*",
    firmware_pattern=r".*",
    description="MAXPOWER 120kW DC fast charger",

    # This charger sends energy in kWh, not Wh (non-standard)
    energy_unit="kWh",
    sends_power_measurand=True,
    power_unit="W",

    # Does NOT respond to WebSocket ping frames
    # → Must disable WS-level pings, rely on OCPP heartbeats only
    # (see server.py: ping_interval=None)

    # On reconnect after a network drop, sends StopTransaction(reason="Other")
    # even if the session was still running on the charger side
    # → Log a warning, don't blindly finalize the session
    stop_reason_on_reboot="Other",
    resumes_session_after_reconnect=True,

    max_power_kw=120.0,
    ramp_time_sec=30,
    power_tapers_above_soc=80,

    smart_charging_safe=True,

    quirks=[
        "Does not respond to WebSocket-level ping frames — use OCPP heartbeats only",
        "Sends energy in kWh not Wh — multiply by 1000 before storing",
        "Sends StopTransaction(reason=Other) on reconnect — not a real session stop",
        "Slow to send StatusNotification after boot — allow up to 10s",
    ],
)
```

The `quirks` list is documentation — it tells future maintainers what to watch for. The boolean and numeric fields are what the handler actually acts on.

## Creating a Custom Profile

### Option 1: Python (for code-based configuration)

```python
from charger_profiles.registry import register_profile, ChargerProfile

MY_CHARGER = ChargerProfile(
    id="acme-fastcharge-v2",
    vendor="ACME",
    model_pattern=r"FastCharge.*",
    firmware_pattern=r"FW[23]\.\d+\.\d+",
    description="ACME FastCharge series (FW2.x and FW3.x)",

    sends_power_measurand=True,
    power_unit="W",
    energy_unit="Wh",
    meter_interval_sec=30,
    soc_available=True,

    max_power_kw=50.0,
    ramp_time_sec=20,
    power_tapers_above_soc=80,

    sends_status_on_boot=True,
    smart_charging_safe=True,

    quirks=[
        "Sends StatusNotification for connector 0 (charger-level) — ignore for billing",
        "Heartbeat drift can be up to 20% — don't flag as offline too quickly",
    ],
)

register_profile(MY_CHARGER)
```

Call `register_profile()` before the first charger connects (e.g., in your app startup code or at module import time).

### Option 2: YAML file

Set `CHARGER_PROFILES_YAML=/etc/ocpp/profiles.yaml` in your `.env`, then create the file:

```yaml
profiles:
  - id: acme-fastcharge-v2
    vendor: ACME
    model_pattern: "FastCharge.*"
    firmware_pattern: "FW[23]\\.\\d+\\.\\d+"
    description: "ACME FastCharge series"
    sends_power_measurand: true
    power_unit: "W"
    energy_unit: "Wh"
    max_power_kw: 50.0
    ramp_time_sec: 20
    smart_charging_safe: true
    quirks:
      - "Sends StatusNotification for connector 0 — ignore for billing"

  - id: budget-ac-charger
    vendor: BUDGETEV
    model_pattern: ".*"
    description: "BudgetEV AC chargers — all models"
    sends_power_measurand: false
    max_power_kw: 11.0
    sends_status_on_boot: false  # Need TriggerMessage to get connector status
    quirks:
      - "Never sends StatusNotification on boot — use TriggerMessage after BootNotification"
```

All fields from `ChargerProfile` are supported. Only `id`, `vendor`, and `model_pattern` are required; everything else uses the generic defaults.

### Option 3: Load programmatically

```python
from charger_profiles.registry import load_profiles_from_yaml

count = load_profiles_from_yaml("/etc/ocpp/my-profiles.yaml")
print(f"Loaded {count} profiles")
```

## Profile Resolution at Runtime

After loading, you can look up profiles:

```python
from charger_profiles.registry import get_profile, list_profiles, resolve_profile

# Get by ID
profile = get_profile("acme-fastcharge-v2")

# List all registered profiles
for p in list_profiles():
    print(p.id, p.vendor, p.model_pattern)

# Resolve for a connected charger
profile = resolve_profile(
    vendor="ACME",
    model="FastCharge-50kW",
    firmware="FW2.4.1"
)
```

## Generic Fallback

If no profile matches, `GENERIC_OCPP16` is used. It has conservative defaults that assume the charger may not send power readings, may not send status on boot, and has `max_power_kw=22.0` (AC charger assumption).

If you're seeing unexpected behavior for a new charger model, check the resolved profile ID in Redis (`charger:{cp_id}` → `profile` field) or in the startup logs:

```
INFO  [CP-001] Boot: MYVENDOR MyModel-50 (fw: FW1.2.3) profile=generic-ocpp16
```

If it says `generic-ocpp16`, no profile matched. Add one.

## Disabling Built-in Examples

The repository ships with example profiles for a few charger families. To disable them and start clean:

```env
CHARGER_PROFILES_NO_EXAMPLES=true
```

Then load only your own profiles via `CHARGER_PROFILES_YAML`.
