"""Configuration management for A23 Agent."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    SCRATCH_DIR: Path = Field(
        default=Path("./scratch"),
        description="Directory root for local computation and staging",
    )
    HTTP_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        description="Default HTTP timeout in seconds",
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
