"""
OCPI 2.2.1 Data Models — Pydantic schemas.

Based on OCPI 2.2.1 specification. Used for API request/response validation
and for data exchange with roaming partners.
"""
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────

class Role(str, Enum):
    CPO = "CPO"
    EMSP = "EMSP"
    HUB = "HUB"
    NAP = "NAP"
    NSP = "NSP"
    OTHER = "OTHER"
    SCSP = "SCSP"


class Status(str, Enum):
    AVAILABLE = "AVAILABLE"
    BLOCKED = "BLOCKED"
    CHARGING = "CHARGING"
    INOPERATIVE = "INOPERATIVE"
    OUTOFORDER = "OUTOFORDER"
    PLANNED = "PLANNED"
    REMOVED = "REMOVED"
    RESERVED = "RESERVED"
    UNKNOWN = "UNKNOWN"


class ConnectorType(str, Enum):
    CHADEMO = "CHADEMO"
    DOMESTIC_A = "DOMESTIC_A"
    DOMESTIC_B = "DOMESTIC_B"
    DOMESTIC_C = "DOMESTIC_C"
    DOMESTIC_D = "DOMESTIC_D"
    DOMESTIC_E = "DOMESTIC_E"
    DOMESTIC_F = "DOMESTIC_F"
    IEC_62196_T1 = "IEC_62196_T1"        # Type 1
    IEC_62196_T1_COMBO = "IEC_62196_T1_COMBO"  # CCS1
    IEC_62196_T2 = "IEC_62196_T2"        # Type 2
    IEC_62196_T2_COMBO = "IEC_62196_T2_COMBO"  # CCS2
    IEC_62196_T3A = "IEC_62196_T3A"
    IEC_62196_T3C = "IEC_62196_T3C"
    TESLA_R = "TESLA_R"
    TESLA_S = "TESLA_S"


class PowerType(str, Enum):
    AC_1_PHASE = "AC_1_PHASE"
    AC_3_PHASE = "AC_3_PHASE"
    DC = "DC"


class TokenType(str, Enum):
    AD_HOC_USER = "AD_HOC_USER"
    APP_USER = "APP_USER"
    OTHER = "OTHER"
    RFID = "RFID"


class WhitelistType(str, Enum):
    ALWAYS = "ALWAYS"
    ALLOWED = "ALLOWED"
    ALLOWED_OFFLINE = "ALLOWED_OFFLINE"
    NEVER = "NEVER"


class AuthMethod(str, Enum):
    AUTH_REQUEST = "AUTH_REQUEST"
    COMMAND = "COMMAND"
    WHITELIST = "WHITELIST"


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    INVALID = "INVALID"
    PENDING = "PENDING"
    RESERVATION = "RESERVATION"


class TariffType(str, Enum):
    AD_HOC_PAYMENT = "AD_HOC_PAYMENT"
    PROFILE_CHEAP = "PROFILE_CHEAP"
    PROFILE_FAST = "PROFILE_FAST"
    PROFILE_GREEN = "PROFILE_GREEN"
    REGULAR = "REGULAR"


class CdrDimensionType(str, Enum):
    CURRENT = "CURRENT"
    ENERGY = "ENERGY"
    ENERGY_EXPORT = "ENERGY_EXPORT"
    ENERGY_IMPORT = "ENERGY_IMPORT"
    FLAT = "FLAT"
    MAX_CURRENT = "MAX_CURRENT"
    MIN_CURRENT = "MIN_CURRENT"
    MAX_POWER = "MAX_POWER"
    MIN_POWER = "MIN_POWER"
    PARKING_TIME = "PARKING_TIME"
    RESERVATION_TIME = "RESERVATION_TIME"
    STATE_OF_CHARGE = "STATE_OF_CHARGE"
    TIME = "TIME"


# ── Core Models ──────────────────────────────────────────────────────────

class GeoLocation(BaseModel):
    latitude: str
    longitude: str


class DisplayText(BaseModel):
    language: str = "en"
    text: str


class BusinessDetails(BaseModel):
    name: str
    website: Optional[str] = None
    logo: Optional[str] = None


class CredentialsRole(BaseModel):
    role: Role
    business_details: BusinessDetails
    party_id: str = Field(max_length=3)
    country_code: str = Field(max_length=2)


class Credentials(BaseModel):
    token: str
    url: str
    roles: list[CredentialsRole]


# ── Locations ────────────────────────────────────────────────────────────

class Connector(BaseModel):
    id: str
    standard: ConnectorType
    format: str = "CABLE"       # CABLE or SOCKET
    power_type: PowerType
    max_voltage: int
    max_amperage: int
    max_electric_power: Optional[int] = None
    tariff_ids: list[str] = []
    last_updated: datetime


class EVSE(BaseModel):
    uid: str
    evse_id: Optional[str] = None   # e.g., "NL*STM*E001*1"
    status: Status
    connectors: list[Connector]
    floor_level: Optional[str] = None
    physical_reference: Optional[str] = None
    last_updated: datetime


class Location(BaseModel):
    country_code: str = "NL"
    party_id: str = "STM"
    id: str
    publish: bool = True
    name: Optional[str] = None
    address: str
    city: str
    postal_code: Optional[str] = None
    country: str = "NLD"
    coordinates: GeoLocation
    evses: list[EVSE] = []
    time_zone: str = "Europe/Amsterdam"
    last_updated: datetime


# ── Sessions ─────────────────────────────────────────────────────────────

class CdrToken(BaseModel):
    uid: str
    type: TokenType = TokenType.RFID
    contract_id: str


class ChargingPeriod(BaseModel):
    start_date_time: datetime
    dimensions: list[dict]
    tariff_id: Optional[str] = None


class Session(BaseModel):
    country_code: str = "NL"
    party_id: str = "STM"
    id: str
    start_date_time: datetime
    end_date_time: Optional[datetime] = None
    kwh: float
    cdr_token: CdrToken
    auth_method: AuthMethod
    location_id: str
    evse_uid: str
    connector_id: str
    currency: str = "EUR"
    total_cost: Optional[float] = None
    status: SessionStatus
    last_updated: datetime


# ── Tariffs ──────────────────────────────────────────────────────────────

class PriceComponent(BaseModel):
    type: CdrDimensionType
    price: float
    vat: Optional[float] = None
    step_size: int = 1


class TariffElement(BaseModel):
    price_components: list[PriceComponent]


class Tariff(BaseModel):
    country_code: str = "NL"
    party_id: str = "STM"
    id: str
    currency: str = "EUR"
    type: Optional[TariffType] = None
    elements: list[TariffElement]
    last_updated: datetime


# ── Tokens ───────────────────────────────────────────────────────────────

class Token(BaseModel):
    country_code: str
    party_id: str
    uid: str
    type: TokenType = TokenType.RFID
    contract_id: str
    issuer: str
    valid: bool = True
    whitelist: WhitelistType = WhitelistType.ALWAYS
    language: Optional[str] = None
    last_updated: datetime


# ── CDRs ─────────────────────────────────────────────────────────────────

class CDR(BaseModel):
    country_code: str = "NL"
    party_id: str = "STM"
    id: str
    start_date_time: datetime
    end_date_time: datetime
    cdr_token: CdrToken
    auth_method: AuthMethod
    location: Location
    currency: str = "EUR"
    total_cost: float
    total_energy: float
    total_time: float           # hours
    total_parking_time: Optional[float] = None
    charging_periods: list[ChargingPeriod] = []
    last_updated: datetime


# ── OCPI Response Wrapper ────────────────────────────────────────────────

class OCPIResponse(BaseModel):
    data: Optional[dict | list] = None
    status_code: int = 1000
    status_message: str = "Success"
    timestamp: datetime
