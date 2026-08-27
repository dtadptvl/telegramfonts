"""Configuration management for A23 Agent."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LEASE_SAFETY_MARGIN_SECONDS = 15


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Ordinary Settings construction must never open the non-versioned
        # repo-root dev.vars secret file; that shape is consumed only by the
        # explicit runtime loader at the production composition boundary.
        env_file=(".env", str(Path.home() / ".telefont.env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Cloudflare Queues Config
    CF_ACCOUNT_ID: str = Field(..., description="Cloudflare Account ID")
    CF_QUEUE_ID: str = Field(..., description="Cloudflare Queue ID or Name")
    CF_QUEUES_TOKEN: SecretStr = Field(..., description="Scoped Cloudflare API Token for Queues")

    # Worker Edge Config
    EDGE_BASE_URL: str = Field(..., description="Base URL of Cloudflare Worker Edge")
    A23_NODE_SECRET: SecretStr = Field(..., description="Internal Node Bearer Authentication Secret")
    A23_WORKER_ID: str = Field(
        default_factory=lambda: os.getenv("A23_WORKER_ID") or f"a23-{socket.gethostname()[:20]}-{os.getpid()}",
        description="Unique identifier for this compute worker instance",
    )

    # Operational Settings
    PULL_BATCH_SIZE: int = Field(default=1, ge=1, le=10, description="Messages per pull request")
    VISIBILITY_TIMEOUT_MS: int = Field(
        default=300_000,
        ge=10_000,
        le=1_800_000,
        description="Queue message visibility timeout in milliseconds",
    )
    HEARTBEAT_INTERVAL_SECONDS: int = Field(
        default=60,
        ge=1,
        le=600,
        description="Interval between lease heartbeats in seconds",
    )
    LEASE_DURATION_SECONDS: int = Field(
        default=300,
        ge=10,
        le=1800,
        description="Requested lease duration in seconds",
    )
    IDLE_BACKOFF_SECONDS: float = Field(
        default=3.0,
        ge=0.5,
        le=60.0,
        description="Idle sleep seconds when no queue messages are returned",
    )
    ERROR_BACKOFF_SECONDS: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Backoff sleep seconds on transient queue pull errors",
    )
    SCRATCH_DIR: Path = Field(
        default=Path("./scratch"),
        description="Directory root for local computation and staging",
    )
    FONT_ARCHIVE_ROOT: Path | None = Field(
        default=None,
        description="External ext4 root for immutable validated OTF/TTF final-font artifacts",
    )
    HTTP_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        description="Default HTTP timeout in seconds",
    )

    # Stage 9D acquisition provider settings (runtime secrets; never logged)
    ACQUISITION_ENABLED: bool = Field(
        default=False,
        description="Enable authorized acquisition cascade (dump-dom -> session -> raster)",
    )
    AUTHORIZED_SESSION_MATERIAL_FILE: Path | None = Field(
        default=None,
        description="Runtime secret file with opaque authorized-session material",
    )
    MONOTYPE_RASTER_ENDPOINT_URL: str = Field(
        default="https://sig.monotype.com",
        description="Authorized Monotype raster endpoint URL",
    )
    MONOTYPE_RASTER_TOKEN: SecretStr | None = Field(
        default=None,
        description="Authorized Monotype raster endpoint bearer token (runtime secret)",
    )
    # Playwright Stealth persistent Chrome fallback settings
    PLAYWRIGHT_USER_DATA_DIR: Path | None = Field(
        default=None,
        description="Persistent profile directory path for Playwright Stealth (retains cf_clearance)",
    )
    PLAYWRIGHT_STEALTH_ENABLED: bool = Field(
        default=True,
        description="Enable Playwright Stealth persistent Chrome fallback",
    )

    # MyFonts Algolia metadata search fallback settings (runtime-only secrets)
    MYFONTS_ALGOLIA_APP_ID: str = Field(
        default="N9095TCBC5",
        description="MyFonts Algolia application ID",
    )
    MYFONTS_ALGOLIA_API_KEY: SecretStr | None = Field(
        default=None,
        description="MyFonts Algolia Search API Key (runtime-only; never logged)",
    )
    MYFONTS_ALGOLIA_INDEX_NAME: str = Field(
        default="prod_myfonts_fonts",
        description="MyFonts Algolia fonts index name",
    )

    VIETNAMESE_AI_ENABLED: bool = Field(
        default=False,
        description="Enable AI-assisted Vietnamese extension (VIETNAMESE mode only)",
    )
    OPENROUTER_API_KEY: SecretStr | None = Field(
        default=None,
        description="OpenRouter runtime API key (runtime secret; never logged)",
    )
    WOKUSHOP_API_KEY: SecretStr | None = Field(
        default=None,
        description="Woku runtime API key (runtime secret; never logged)",
    )

    @field_validator("EDGE_BASE_URL")
    @classmethod
    def normalize_edge_url(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("A23_WORKER_ID")
    @classmethod
    def sanitize_worker_id(cls, v: str) -> str:
        clean = "".join(c for c in v.strip() if c.isalnum() or c in ("-", "_"))
        if not clean:
            clean = "a23-worker"
        return clean[:64]

    @model_validator(mode="after")
    def validate_heartbeat_lease_safety(self) -> Settings:
        # Cadence + 15s safety margin must be strictly less than lease duration (BLOCK D)
        if self.HEARTBEAT_INTERVAL_SECONDS + LEASE_SAFETY_MARGIN_SECONDS >= self.LEASE_DURATION_SECONDS:
            raise ValueError(
                f"Unsafe configuration: HEARTBEAT_INTERVAL_SECONDS ({self.HEARTBEAT_INTERVAL_SECONDS}) + "
                f"{LEASE_SAFETY_MARGIN_SECONDS}s safety margin must be less than LEASE_DURATION_SECONDS ({self.LEASE_DURATION_SECONDS})"
            )
        return self
