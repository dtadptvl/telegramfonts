"""Authorized source acquisition subsystem.

Implements the approved provider order:
1. native Chrome `--headless=new --dump-dom` binary retrieval (primary);
2. authorized persistent Chrome/session (`cf_clearance`) binary retrieval (fallback);
3. authorized Monotype raster endpoint (fallback, raster evidence only).

A valid authorized binary wins at any stage and skips geometry reconstruction.
All transports/providers are injectable and testable without real credentials.
Secrets/session material never enter logs, exceptions, cache keys, or artifacts.
"""
from acquisition.models import (  # noqa: F401
    AcquisitionOutcome,
    AcquisitionStageRecord,
    AcquisitionTrace,
    AcquiredBinary,
    BinaryAcquisitionPolicy,
)
from acquisition.providers import (  # noqa: F401
    AuthorizedRasterClient,
    AuthorizedSessionMaterialProvider,
    DumpDomTransport,
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    SpriteRasterPage,
)
from acquisition.pipeline import AcquisitionPipeline  # noqa: F401
from acquisition.verifier import verify_acquired_binary  # noqa: F401
