"""Ordered, traced acquisition pipeline with full 4-lane fallback graph.

Lanes:
  Method 1 (Primary): Native Chrome/Edge `--headless=new --dump-dom` preflight
  Method 2 (Fallback 1): Playwright Stealth real-Chrome persistent context
  Method 3 (Fallback 2): Direct Monotype CDN multi-page / multi-size crawl
  Method 4 (Fallback 3): MyFonts Algolia metadata discovery -> CDN lane

Invariants:
- A valid authorized binary always wins immediately and halts all later raster work.
- Incomplete raster evidence is insufficient and triggers ordered fallback exhaustion.
- Discovery envelope is family-scoped and reused across all styles in an order.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from acquisition.models import (
    AcquisitionOutcome,
    AcquisitionStageRecord,
    AcquisitionTrace,
    AcquiredBinary,
    BinaryAcquisitionPolicy,
    BINARY_STAGE_AUTHORIZED_SESSION,
    BINARY_STAGE_DUMP_DOM,
    STAGE_DUMP_DOM_NATIVE,
    STAGE_PLAYWRIGHT_STEALTH,
    STAGE_DIRECT_MONOTYPE_CDN,
    STAGE_ALGOLIA_METADATA_CDN,
    DiscoveryEnvelope,
    FamilyDiscoveryEnvelope,
    StyleDiscoveryRecord,
    RASTER_STAGE_MONOTYPE_ENDPOINT,
    is_complete_raster_pages,
)
from acquisition.providers import (
    DumpDomTransport,
    MonotypeRasterProvider,
    PersistentSessionBinaryProvider,
    PlaywrightStealthProvider,
    AlgoliaMetadataProvider,
    extract_binary_from_dump_dom,
    parse_family_discovery_from_dump,
    parse_discovery_from_dump,
)
from acquisition.verifier import verify_acquired_binary

logger = logging.getLogger("telegramfonts.agent.acquisition.pipeline")


class AcquisitionPipeline:
    def __init__(
        self,
        dump_dom_transport: DumpDomTransport | None = None,
        binary_fetch: Callable[[str], Awaitable[bytes | None]] | None = None,
        session_provider: PersistentSessionBinaryProvider | None = None,
        raster_provider: MonotypeRasterProvider | None = None,
        playwright_provider: PlaywrightStealthProvider | None = None,
        algolia_provider: AlgoliaMetadataProvider | None = None,
        policy: BinaryAcquisitionPolicy | None = None,
    ) -> None:
        self.dump_dom_transport = dump_dom_transport
        self.binary_fetch = binary_fetch
        self.session_provider = session_provider
        self.raster_provider = raster_provider
        self.playwright_provider = playwright_provider
        self.algolia_provider = algolia_provider
        self.policy = policy or BinaryAcquisitionPolicy()
        self._cached_family_envelopes: dict[str, FamilyDiscoveryEnvelope] = {}
        self._cached_dumps: dict[str, str] = {}

    async def _binary_fetch_stub(self, url: str) -> bytes | None:
        if self.binary_fetch is None:
            return None
        return await self.binary_fetch(url)

    async def acquire_family_preflight(
        self,
        source_url: str,
        expected_family: str = "",
        expected_styles: list[Any] | None = None,
    ) -> FamilyDiscoveryEnvelope:
        """Run single shared family preflight across discovery lanes until complete map."""
        if source_url in self._cached_family_envelopes:
            cached = self._cached_family_envelopes[source_url]
            if expected_styles is None or cached.has_complete_map_for(expected_styles):
                return cached

        best_envelope: FamilyDiscoveryEnvelope | None = None

        # Lane 1: Native Dump-DOM
        if self.dump_dom_transport is not None:
            try:
                dump = await self.dump_dom_transport.dump_dom(source_url)
                if dump:
                    self._cached_dumps[source_url] = dump
                    best_envelope = parse_family_discovery_from_dump(dump, source_url, STAGE_DUMP_DOM_NATIVE)
                    if expected_styles is None or best_envelope.has_complete_map_for(expected_styles):
                        self._cached_family_envelopes[source_url] = best_envelope
                        return best_envelope
            except Exception as exc:
                logger.debug("Dump-dom family preflight exception: %s", exc)

        # Lane 2: Playwright Stealth Persistent
        if self.playwright_provider is not None and self.policy.playwright_stealth_enabled and self.playwright_provider.available():
            try:
                env2 = await self.playwright_provider.discover_family(source_url)
                if env2 is not None:
                    if best_envelope is None or len(env2.styles) > len(best_envelope.styles):
                        best_envelope = env2
                    if expected_styles is None or env2.has_complete_map_for(expected_styles):
                        self._cached_family_envelopes[source_url] = env2
                        return env2
            except Exception as exc:
                logger.debug("Playwright stealth family preflight exception: %s", exc)

        final_env = best_envelope or FamilyDiscoveryEnvelope(
            family_name=expected_family, family_url=source_url, provenance="none"
        )
        self._cached_family_envelopes[source_url] = final_env
        return final_env

    async def acquire(
        self,
        source_url: str,
        expected_family: str,
        expected_style: str,
        raster_request: dict[str, Any] | None = None,
        family_envelope: FamilyDiscoveryEnvelope | None = None,
    ) -> AcquisitionOutcome:
        """Execute ordered fallback for one style, reusing the family discovery envelope."""
        records: list[AcquisitionStageRecord] = []

        if family_envelope is None:
            family_envelope = await self.acquire_family_preflight(source_url, expected_family)

        style_rec = family_envelope.get_style_record(expected_style, expected_style)
        style_md5 = style_rec.md5 if style_rec else ""

        legacy_envelope = DiscoveryEnvelope(
            family_name=family_envelope.family_name,
            style_name=(style_rec.style_name if style_rec else "") or expected_style,
            md5=style_md5,
            raster_identity=style_md5,
            binary_candidates=style_rec.binary_candidates if style_rec else (),
            provenance=family_envelope.provenance,
        )

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
                discovery=legacy_envelope,
                family_discovery=family_envelope,
            )

        # Cross-family validation (fail closed)
        if family_envelope.family_name and expected_family and family_envelope.family_name.lower().strip() != expected_family.lower().strip():
            records.append(
                AcquisitionStageRecord(
                    stage=STAGE_DUMP_DOM_NATIVE,
                    attempted=True,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="INTEGRITY_FAILED",
                    reason_code="DISCOVERY_FAMILY_MISMATCH",
                )
            )
            return outcome_with("insufficient", terminal="ACQUISITION_BINARY_INTEGRITY_FAILED:DISCOVERY_FAMILY_MISMATCH")

        style_rec = family_envelope.get_style_record(expected_style, expected_style)

        # -------------------------------------------------------------
        # STEP 1: Check Binary-First from Dump / Envelope candidates
        # -------------------------------------------------------------
        dump = self._cached_dumps.get(source_url, "")
        if not dump and self.dump_dom_transport is not None:
            try:
                dump = await self.dump_dom_transport.dump_dom(source_url)
                if dump:
                    self._cached_dumps[source_url] = dump
            except Exception:
                dump = ""

        raw_binary: bytes | None = None
        if style_rec and style_rec.binary_candidates:
            try:
                raw_binary = await extract_binary_from_dump_dom(
                    dump, self._binary_fetch_stub, self.policy,
                    DiscoveryEnvelope(
                        family_name=family_envelope.family_name,
                        style_name=style_rec.style_name,
                        binary_candidates=style_rec.binary_candidates,
                        provenance=style_rec.provenance,
                    )
                )
            except Exception:
                raw_binary = None

        if raw_binary is not None:
            verification = verify_acquired_binary(raw_binary, expected_family, expected_style, self.policy.max_binary_bytes)
            if verification.status == "VALID":
                records.append(
                    AcquisitionStageRecord(
                        stage=STAGE_DUMP_DOM_NATIVE,
                        attempted=True,
                        produced_binary=True,
                        produced_raster=False,
                        outcome="OK",
                    )
                )
                binary = AcquiredBinary(
                    raw_bytes=raw_binary,
                    format=verification.format,
                    family_name=verification.family_name,
                    style_name=verification.style_name,
                    provenance=STAGE_DUMP_DOM_NATIVE,
                )
                return outcome_with("binary", binary=binary)
            if verification.status == "INTEGRITY_FAILED":
                records.append(
                    AcquisitionStageRecord(
                        stage=STAGE_DUMP_DOM_NATIVE,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="INTEGRITY_FAILED",
                        reason_code=verification.reason_code,
                    )
                )
                return outcome_with("insufficient", terminal=f"ACQUISITION_BINARY_INTEGRITY_FAILED:{verification.reason_code}")

        records.append(
            AcquisitionStageRecord(
                stage=STAGE_DUMP_DOM_NATIVE,
                attempted=bool(self.dump_dom_transport is not None),
                produced_binary=False,
                produced_raster=False,
                outcome="BINARY_ABSENT" if self.dump_dom_transport else "DISABLED",
                reason_code="BINARY_ABSENT",
            )
        )

        # -------------------------------------------------------------
        # STEP 2: Playwright Stealth Persistent Context (Method 2)
        # -------------------------------------------------------------
        req_pts_raw = (raster_request or {}).get("acs_pts")
        req_pts = [int(p) for p in req_pts_raw] if req_pts_raw is not None else None
        stealth_pts = req_pts or [int((raster_request or {}).get("acs_pt", 120))]

        if self.policy.playwright_stealth_enabled and self.playwright_provider is not None and self.playwright_provider.available():
            pages = ()
            try:
                pages = await self.playwright_provider.capture_raster_pages(
                    source_url, style_rec or StyleDiscoveryRecord(style_id=expected_style, style_name=expected_style), stealth_pts
                ) or ()
            except Exception as exc:
                logger.debug("Playwright raster capture exception: %s", exc)
                pages = ()

            if pages and is_complete_raster_pages(pages, req_pts, expected_md5=style_rec.md5 if (style_rec and style_rec.md5) else ""):
                records.append(
                    AcquisitionStageRecord(
                        stage=STAGE_PLAYWRIGHT_STEALTH,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=True,
                        outcome="OK",
                    )
                )
                return outcome_with("raster_authorized", pages=pages)
            else:
                records.append(
                    AcquisitionStageRecord(
                        stage=STAGE_PLAYWRIGHT_STEALTH,
                        attempted=True,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="RASTER_ABSENT",
                        reason_code="STEALTH_RASTER_INCOMPLETE_OR_UNAVAILABLE",
                    )
                )

        # Legacy Session Provider fallback (for backward-compatibility with stage 9D test fixtures)
        if self.policy.authorized_session_enabled and self.session_provider is not None and self.session_provider.available():
            try:
                raw_session = await self.session_provider.fetch_binary_for_envelope(
                    legacy_envelope, source_url, self.policy
                )
            except Exception:
                raw_session = None
            if raw_session is not None:
                verification = verify_acquired_binary(raw_session, expected_family, expected_style, self.policy.max_binary_bytes)
                if verification.status == "VALID":
                    records.append(
                        AcquisitionStageRecord(
                            stage=BINARY_STAGE_AUTHORIZED_SESSION,
                            attempted=True,
                            produced_binary=True,
                            produced_raster=False,
                            outcome="OK",
                        )
                    )
                    binary = AcquiredBinary(
                        raw_bytes=raw_session,
                        format=verification.format,
                        family_name=verification.family_name,
                        style_name=verification.style_name,
                        provenance=BINARY_STAGE_AUTHORIZED_SESSION,
                    )
                    return outcome_with("binary", binary=binary)
                if verification.status == "INTEGRITY_FAILED":
                    records.append(
                        AcquisitionStageRecord(
                            stage=BINARY_STAGE_AUTHORIZED_SESSION,
                            attempted=True,
                            produced_binary=False,
                            produced_raster=False,
                            outcome="INTEGRITY_FAILED",
                            reason_code=verification.reason_code,
                        )
                    )
                    return outcome_with("insufficient", terminal=f"ACQUISITION_BINARY_INTEGRITY_FAILED:{verification.reason_code}")
            records.append(
                AcquisitionStageRecord(
                    stage=BINARY_STAGE_AUTHORIZED_SESSION,
                    attempted=True,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="BINARY_ABSENT",
                    reason_code="BINARY_ABSENT",
                )
            )

        # -------------------------------------------------------------
        # STEP 3: Direct Monotype CDN Raster Ingestion (Method 3)
        # -------------------------------------------------------------
        style_md5 = style_rec.md5 if style_rec else ""
        if not style_md5 and legacy_envelope.md5:
            style_md5 = legacy_envelope.md5
        if not style_md5 and raster_request:
            style_md5 = str(raster_request.get("md5", "")).strip().lower()

        if self.policy.monotype_raster_enabled and self.raster_provider is not None and self.raster_provider.available():
            if not style_md5 or len(style_md5) != 32:
                records.append(
                    AcquisitionStageRecord(
                        stage=STAGE_DIRECT_MONOTYPE_CDN,
                        attempted=False,
                        produced_binary=False,
                        produced_raster=False,
                        outcome="RASTER_ABSENT",
                        reason_code="MISSING_STYLE_MD5",
                    )
                )
            else:
                target = dict(raster_request or {})
                target["family"] = family_envelope.family_name or expected_family
                target["style"] = (style_rec.style_name if style_rec else "") or expected_style
                target["md5"] = style_md5
                target["acs_pts"] = req_pts

                pages: tuple = ()
                try:
                    client = getattr(self.raster_provider, "client", None)
                    if hasattr(client, "fetch_all_sprite_pages"):
                        pages = await client.fetch_all_sprite_pages(target, self.policy) or ()
                    else:
                        pages = await self.raster_provider.fetch_sprite_pages(target, self.policy)
                except Exception as exc:
                    logger.debug("Monotype CDN crawl exception: %s", exc)
                    pages = ()

                if pages and is_complete_raster_pages(pages, req_pts, expected_md5=style_md5):
                    records.append(
                        AcquisitionStageRecord(
                            stage=STAGE_DIRECT_MONOTYPE_CDN,
                            attempted=True,
                            produced_binary=False,
                            produced_raster=True,
                            outcome="OK",
                        )
                    )
                    return outcome_with("raster_authorized", pages=pages)
                else:
                    records.append(
                        AcquisitionStageRecord(
                            stage=STAGE_DIRECT_MONOTYPE_CDN,
                            attempted=True,
                            produced_binary=False,
                            produced_raster=False,
                            outcome="RASTER_ABSENT",
                            reason_code="CDN_RASTER_PARTIAL_OR_EMPTY",
                        )
                    )
        else:
            records.append(
                AcquisitionStageRecord(
                    stage=STAGE_DIRECT_MONOTYPE_CDN,
                    attempted=False,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="DISABLED",
                    reason_code="CAPABILITY_UNAVAILABLE",
                )
            )

        # -------------------------------------------------------------
        # STEP 4: Algolia Metadata Search -> Monotype CDN Ingestion (Method 4)
        # -------------------------------------------------------------
        if (
            self.policy.algolia_enabled
            and self.algolia_provider is not None
            and self.algolia_provider.available()
            and self.policy.monotype_raster_enabled
            and self.raster_provider is not None
            and self.raster_provider.available()
        ):
            fam_query = expected_family or (family_envelope.family_name if family_envelope else "")
            if not fam_query:
                slug = source_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                fam_query = slug.replace("-", " ").replace("_", " ")

            algolia_env = None
            try:
                algolia_env = await self.algolia_provider.discover_family(fam_query, source_url)
            except Exception as exc:
                logger.debug("Algolia metadata discovery exception: %s", exc)
                algolia_env = None

            alg_rec = algolia_env.get_style_record(expected_style, expected_style) if algolia_env else None
            alg_md5 = alg_rec.md5 if alg_rec else ""

            if alg_md5 and len(alg_md5) == 32:
                target = dict(raster_request or {})
                target["family"] = (algolia_env.family_name if algolia_env else "") or expected_family
                target["style"] = (alg_rec.style_name if alg_rec else "") or expected_style
                target["md5"] = alg_md5
                target["acs_pts"] = req_pts

                pages = ()
                try:
                    client = getattr(self.raster_provider, "client", None)
                    if hasattr(client, "fetch_all_sprite_pages"):
                        pages = await client.fetch_all_sprite_pages(target, self.policy) or ()
                    else:
                        pages = await self.raster_provider.fetch_sprite_pages(target, self.policy)
                except Exception as exc:
                    logger.debug("Algolia-to-CDN crawl exception: %s", exc)
                    pages = ()

                if pages and is_complete_raster_pages(pages, req_pts, expected_md5=alg_md5):
                    records.append(
                        AcquisitionStageRecord(
                            stage=STAGE_ALGOLIA_METADATA_CDN,
                            attempted=True,
                            produced_binary=False,
                            produced_raster=True,
                            outcome="OK",
                        )
                    )
                    return outcome_with("raster_authorized", pages=pages)

            records.append(
                AcquisitionStageRecord(
                    stage=STAGE_ALGOLIA_METADATA_CDN,
                    attempted=True,
                    produced_binary=False,
                    produced_raster=False,
                    outcome="RASTER_ABSENT",
                    reason_code="ALGOLIA_METADATA_OR_CDN_UNAVAILABLE",
                )
            )

        return outcome_with("insufficient", terminal="ACQUISITION_INSUFFICIENT")
