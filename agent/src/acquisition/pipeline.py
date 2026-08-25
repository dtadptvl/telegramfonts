"""Ordered, traced acquisition pipeline: dump-dom -> authorized session -> authorized raster.

A fallback stage starts only after the preceding capability proves binary/raster
insufficiency. A valid authorized binary wins immediately. Integrity failure is
fail-closed terminal and never silently falls through to a later stage.

The dump-dom stage produces a typed discovery envelope (canonical family/style,
authorized binary candidates including WOFF/WOFF2 containers, MD5/raster
identity, provenance) that drives every later stage. The raster stage only
runs with an exact family/style/MD5 target; a missing target fails closed.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from acquisition.models import (
    AcquisitionOutcome,
    AcquisitionStageRecord,
    AcquisitionTrace,
    AcquiredBinary,
    BinaryAcquisitionPolicy,
    BINARY_STAGE_AUTHORIZED_SESSION,
    BINARY_STAGE_DUMP_DOM,
    DiscoveryEnvelope,
    RASTER_STAGE_MONOTYPE_ENDPOINT,
)
from acquisition.providers import (
    DumpDomTransport,
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    extract_binary_from_dump_dom,
    parse_discovery_from_dump,
)
from acquisition.verifier import verify_acquired_binary


class AcquisitionPipeline:
    def __init__(
        self,
        dump_dom_transport: DumpDomTransport | None = None,
        binary_fetch: Callable[[str], Awaitable[bytes | None]] | None = None,
        session_provider: PersistentSessionBinaryProvider | None = None,
        raster_provider: MonotypeRasterProvider | None = None,
        policy: BinaryAcquisitionPolicy | None = None,
    ) -> None:
        self.dump_dom_transport = dump_dom_transport
        self.binary_fetch = binary_fetch
        self.session_provider = session_provider
        self.raster_provider = raster_provider
        self.policy = policy or BinaryAcquisitionPolicy()

    async def _binary_fetch_stub(self, url: str) -> bytes | None:
        if self.binary_fetch is None:
            return None
        return await self.binary_fetch(url)

    async def acquire(
        self,
        source_url: str,
        expected_family: str,
        expected_style: str,
        raster_request: dict[str, Any] | None = None,
    ) -> AcquisitionOutcome:
        records: list[AcquisitionStageRecord] = []
        envelope: DiscoveryEnvelope | None = None

        def outcome_with(
            kind: str,
            binary: AcquiredBinary | None = None,
            pages: tuple = (),
            terminal: str = "",
        ) -> AcquisitionOutcome:
            return AcquisitionOutcome(
                kind=kind,
                binary=binary,
                raster_pages=pages,
                trace=AcquisitionTrace(records=tuple(records)),
                terminal_reason_code=terminal,
                discovery=envelope,
            )

        async def attempt_binary_stage(stage: str, raw: bytes | None) -> AcquisitionOutcome | None:
            """Verify one stage's bytes. Returns terminal outcome or None to continue."""
            verification = verify_acquired_binary(raw, expected_family, expected_style, self.policy.max_binary_bytes)
            if verification.status == "VALID":
                records.append(
                    AcquisitionStageRecord(
                        stage=stage, attempted=True, produced_binary=True, produced_raster=False, outcome="OK"
                    )
                )
                binary = AcquiredBinary(
                    raw_bytes=raw,
                    format=verification.format,
                    family_name=verification.family_name,
                    style_name=verification.style_name,
                    provenance=stage,
                )
                return outcome_with("binary", binary=binary)
            if verification.status == "INTEGRITY_FAILED":
                records.append(
                    AcquisitionStageRecord(
                        stage=stage,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="INTEGRITY_FAILED",
                        reason_code=verification.reason_code,
                    )
                )
                return outcome_with(
                    "insufficient",
                    terminal=f"ACQUISITION_BINARY_INTEGRITY_FAILED:{verification.reason_code}",
                )
            records.append(
                AcquisitionStageRecord(
                    stage=stage,
                    attempted=True,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="BINARY_ABSENT",
                    reason_code="BINARY_ABSENT",
                )
            )
            return None

        # Stage 1: native Chrome headless dump-dom (primary) -> typed discovery
        # envelope driving all later stages.
        raw_dump_dom: bytes | None = None
        if self.dump_dom_transport is not None:
            dump = ""
            try:
                dump = await self.dump_dom_transport.dump_dom(source_url)
            except Exception:
                dump = ""
            envelope = parse_discovery_from_dump(dump, source_url, BINARY_STAGE_DUMP_DOM)
            if envelope.family_name and expected_family and envelope.family_name.lower() != expected_family.lower():
                # Cross-family discovery is a fail-closed identity mismatch.
                records.append(
                    AcquisitionStageRecord(
                        stage=BINARY_STAGE_DUMP_DOM,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="INTEGRITY_FAILED",
                        reason_code="DISCOVERY_FAMILY_MISMATCH",
                    )
                )
                return outcome_with("insufficient", terminal="ACQUISITION_BINARY_INTEGRITY_FAILED:DISCOVERY_FAMILY_MISMATCH")
            try:
                raw_dump_dom = await extract_binary_from_dump_dom(
                    dump, self._binary_fetch_stub, self.policy, envelope
                )
            except Exception:
                raw_dump_dom = None
            terminal = await attempt_binary_stage(BINARY_STAGE_DUMP_DOM, raw_dump_dom)
            if terminal is not None:
                return terminal
        else:
            records.append(
                AcquisitionStageRecord(
                    stage=BINARY_STAGE_DUMP_DOM,
                    attempted=False,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="DISABLED",
                    reason_code="CAPABILITY_UNAVAILABLE",
                )
            )

        # Stage 2: authorized persistent Chrome/session discovery fallback.
        # Operates on the typed envelope (binary candidates/raster identity);
        # never treats collection-page HTML as font bytes.
        if not self.policy.authorized_session_enabled or self.session_provider is None or not self.session_provider.available():
            records.append(
                AcquisitionStageRecord(
                    stage=BINARY_STAGE_AUTHORIZED_SESSION,
                    attempted=False,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="DISABLED",
                    reason_code="CAPABILITY_UNAVAILABLE",
                )
            )
        else:
            try:
                raw_session = await self.session_provider.fetch_binary_for_envelope(
                    envelope or DiscoveryEnvelope(), source_url, self.policy
                )
            except Exception:
                raw_session = None
            terminal = await attempt_binary_stage(BINARY_STAGE_AUTHORIZED_SESSION, raw_session)
            if terminal is not None:
                return terminal

        # Stage 3: authorized raster endpoint fallback with the exact
        # family/style/MD5 target. Missing target fails closed (no request).
        if not self.policy.monotype_raster_enabled or self.raster_provider is None or not self.raster_provider.available():
            records.append(
                AcquisitionStageRecord(
                    stage=RASTER_STAGE_MONOTYPE_ENDPOINT,
                    attempted=False,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="DISABLED",
                    reason_code="CAPABILITY_UNAVAILABLE",
                )
            )
        else:
            target = dict(raster_request or {})
            if envelope is not None:
                target.setdefault("family", envelope.family_name)
                target.setdefault("style", envelope.style_name or expected_style)
                target.setdefault("md5", envelope.md5)
            target.setdefault("family", expected_family)
            target.setdefault("style", expected_style)
            target.setdefault("md5", "")

            if not (str(target.get("family", "")).strip() and str(target.get("style", "")).strip() and str(target.get("md5", "")).strip()):
                records.append(
                    AcquisitionStageRecord(
                        stage=RASTER_STAGE_MONOTYPE_ENDPOINT,
                        attempted=False,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="RASTER_ABSENT",
                        reason_code="MISSING_RASTER_TARGET",
                    )
                )
            else:
                try:
                    pages = await self.raster_provider.fetch_sprite_pages(target, self.policy)
                except Exception:
                    pages = ()
                if pages and sum(p.glyph_count for p in pages) > 0:
                    records.append(
                        AcquisitionStageRecord(
                            stage=RASTER_STAGE_MONOTYPE_ENDPOINT,
                            attempted=True,
                            produced_binary=False,
                            produced_raster=True,
                            outcome="OK",
                        )
                    )
                    return outcome_with("raster_authorized", pages=pages)
                records.append(
                    AcquisitionStageRecord(
                        stage=RASTER_STAGE_MONOTYPE_ENDPOINT,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="RASTER_ABSENT",
                        reason_code="RASTER_ABSENT",
                    )
                )

        return outcome_with("insufficient", terminal="ACQUISITION_INSUFFICIENT")
