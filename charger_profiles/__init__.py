"""
Charger Profiles — firmware-specific behavior adapters.

Each charger manufacturer/model has quirks. Instead of littering the handler
with if/else chains, we define profiles that describe the behavior and let
the handler consult the profile.

Profile selection: on BootNotification, match vendor+model+firmware to a profile.
Stored in Redis as charger:{cp_id}:profile for fast access.
"""
