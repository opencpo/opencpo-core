"""
OCPP Core — Configuration

All config via environment variables. No hardcoded values.
dotenv is loaded before this module is used (see main.py).
"""
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int = 0) -> int:
    return int(os.environ.get(key, str(default)))


def _env_float(key: str, default: float = 0.0) -> float:
    return float(os.environ.get(key, str(default)))


def _env_bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class DatabaseConfig:
    @property
    def host(self): return _env("PG_HOST", "127.0.0.1")
    @property
    def port(self): return _env_int("PG_PORT", 5432)
    @property
    def name(self): return _env("PG_NAME", "ocpp")
    @property
    def user(self): return _env("PG_USER", "ocpp")
    @property
    def password(self): return _env("PG_PASSWORD", "")
    @property
    def min_pool(self): return _env_int("PG_POOL_MIN", 5)
    @property
    def max_pool(self): return _env_int("PG_POOL_MAX", 20)
    @property
    def replica_host(self): return _env("PG_REPLICA_HOST", "")
    @property
    def replica_port(self): return _env_int("PG_REPLICA_PORT", 5432)

@dataclass
class RedisConfig:
    @property
    def host(self): return _env("REDIS_HOST", "127.0.0.1")
    @property
    def port(self): return _env_int("REDIS_PORT", 6380)
    @property
    def db(self): return _env_int("REDIS_DB", 2)
    @property
    def password(self): return _env("REDIS_PASSWORD", "")
    @property
    def url(self):
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


@dataclass
class OCPPConfig:
    @property
    def ocpp16_host(self): return _env("OCPP16_HOST", "0.0.0.0")
    @property
    def ocpp16_port(self): return _env_int("OCPP16_PORT", 9100)
    @property
    def ocpp201_host(self): return _env("OCPP201_HOST", "0.0.0.0")
    @property
    def ocpp201_port(self): return _env_int("OCPP201_PORT", 9201)
    @property
    def heartbeat_interval(self): return _env_int("OCPP_HEARTBEAT_INTERVAL", 60)
    @property
    def meter_batch_interval(self): return _env_float("OCPP_METER_BATCH_INTERVAL", 2.0)
    @property
    def meter_batch_size(self): return _env_int("OCPP_METER_BATCH_SIZE", 100)


@dataclass
class APIConfig:
    @property
    def host(self): return _env("API_HOST", "0.0.0.0")
    @property
    def port(self): return _env_int("API_PORT", 8000)
    @property
    def api_key(self): return _env("API_KEY", "")
    @property
    def cors_origins(self): return _env("CORS_ORIGINS", "*").split(",")


@dataclass
class PKIConfig:
    @property
    def data_dir(self): return _env("PKI_DATA_DIR", "./data/pki")
    @property
    def root_ca_key_password(self): return _env("PKI_ROOT_CA_PASSWORD", "")
    @property
    def sub_ca_key_password(self): return _env("PKI_SUB_CA_PASSWORD", "")
    @property
    def cert_validity_days(self): return _env_int("PKI_CERT_VALIDITY_DAYS", 365)
    @property
    def ocsp_port(self): return _env_int("PKI_OCSP_PORT", 8099)
    @property
    def org_name(self): return _env("PKI_ORG_NAME", "OCPP Core")
    @property
    def root_ca_cn(self): return _env("PKI_ROOT_CA_CN", "OCPP Core Root CA")
    @property
    def user_ca_cn(self): return _env("PKI_USER_CA_CN", "OCPP Core User CA")


@dataclass
class TailscaleConfig:
    @property
    def enabled(self): return _env_bool("TAILSCALE_ENABLED", False)
    @property
    def api_key(self): return _env("TAILSCALE_API_KEY", "")
    @property
    def tailnet(self): return _env("TAILSCALE_TAILNET", "")
    @property
    def gateway_tag(self): return _env("TAILSCALE_GATEWAY_TAG", "tag:charger-gw")


@dataclass
class EventsConfig:
    @property
    def stream_prefix(self): return _env("EVENTS_STREAM_PREFIX", "ocpp")
    @property
    def max_stream_length(self): return _env_int("EVENTS_MAX_STREAM_LENGTH", 100000)
    @property
    def consumer_block_ms(self): return _env_int("EVENTS_CONSUMER_BLOCK_MS", 5000)


@dataclass
class PushConfig:
    @property
    def service_url(self): return _env("PUSH_SERVICE_URL", "")
    @property
    def api_key(self): return _env("PUSH_INTERNAL_API_KEY", "")


@dataclass
class Config:
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    ocpp: OCPPConfig = field(default_factory=OCPPConfig)
    api: APIConfig = field(default_factory=APIConfig)
    pki: PKIConfig = field(default_factory=PKIConfig)
    tailscale: TailscaleConfig = field(default_factory=TailscaleConfig)
    events: EventsConfig = field(default_factory=EventsConfig)
    push: PushConfig = field(default_factory=PushConfig)

    @property
    def log_level(self): return _env("LOG_LEVEL", "INFO")
    @property
    def log_format(self): return _env("LOG_FORMAT", "json")


# Singleton
config = Config()
