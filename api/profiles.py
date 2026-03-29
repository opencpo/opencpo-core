"""
Charger Profiles API — view and manage charger behavior profiles.
"""
import logging
from dataclasses import asdict

from fastapi import APIRouter

from charger_profiles.registry import list_profiles, get_profile, resolve_profile

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("")
async def get_profiles():
    """List all charger profiles."""
    profiles = list_profiles()
    return {
        "profiles": [
            {
                "id": p.id,
                "vendor": p.vendor,
                "model_pattern": p.model_pattern,
                "description": p.description,
                "max_power_kw": p.max_power_kw,
                "sends_power_measurand": p.sends_power_measurand,
                "quirks": p.quirks,
            }
            for p in profiles
        ]
    }


@router.get("/{profile_id}")
async def get_profile_detail(profile_id: str):
    """Get full details of a charger profile."""
    profile = get_profile(profile_id)
    if not profile:
        return {"error": f"Profile '{profile_id}' not found"}, 404
    return {"profile": asdict(profile)}


@router.get("/resolve/{vendor}/{model}")
async def resolve_charger_profile(vendor: str, model: str, firmware: str = ""):
    """Resolve which profile matches a given charger."""
    profile = resolve_profile(vendor, model, firmware)
    return {
        "matched_profile": profile.id,
        "description": profile.description,
        "vendor": vendor,
        "model": model,
        "firmware": firmware,
    }
