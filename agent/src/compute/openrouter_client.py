"""OpenRouter runtime client implementing the Vietnamese AI provider contract.

Fixed routing (no other model/provider/substitution):
- PRIMARY   google/gemma-3-12b-it        every missing-glyph AI case.
- DIFFICULT google/gemma-3-27b-it        only on deterministic escalation.
- ARBITER   google/gemini-3.1-flash-lite at most once, only after an unresolved
            deterministic disagreement between PRIMARY and DIFFICULT outputs.

Exact cache hits and complete-source/ORIGINAL paths make zero calls (enforced
by the extension service, which never constructs this client in those cases).

Runtime secret only; closed-schema responses; sanitized provenance. Secrets
never enter logs, exceptions, cache keys, or artifacts.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from compute.vietnamese import AICandidateSpec, VietnameseAIIntegrityError

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
MODEL_PRIMARY = "google/gemma-3-12b-it"
MODEL_DIFFICULT = "google/gemma-3-27b-it"
MODEL_ARBITER = "google/gemini-3.1-flash-lite"
ROUTING_VERSION = "openrouter-route-v1"
DIFFICULT_ESCALATION_GLYPH_COUNT = 6

PROMPT_TEMPLATE = (
    "You are a font geometry generator extending an existing font with missing"
    " Vietnamese glyphs in the SAME visual style as the source evidence."
    " Source style evidence (bounded): __STYLE_EVIDENCE__."
    " Emit ONLY a JSON object matching this schema: "
    '{"glyphs":[{"code_point":int,"contours":[[[x,y],...]], '
    '"advance_width_upem":number,"lsb_upem":number,"rsb_upem":number,'
    '"ascent_upem":number,"descent_upem":number,'
    '"anchors":[["name",x,y],...]}]}.'
    " Coordinates are font units (upem=__UPEM__). Family context hash: __SOURCE_HASH__."
    " Missing code points: __CODE_POINTS__. No prose, no extra keys."
)

ARBITER_PROMPT_TEMPLATE = (
    "Two candidate glyph sets A and B were generated for code points __CODE_POINTS__."
    " Candidate evidence A (geometry/metrics/scores): __EVIDENCE_A__."
    " Candidate evidence B (geometry/metrics/scores): __EVIDENCE_B__."
    " Source style evidence for comparison: __STYLE_EVIDENCE__."
    ' Choose the set more consistent with the source style. Reply ONLY with JSON: {"choice":"A"} or {"choice":"B"}.'
)

MAX_EVIDENCE_CHARS = 4000


@dataclass(frozen=True)
class RouteCallRecord:
    """Sanitized provenance for one model call (never includes secrets)."""

    model: str
    role: str  # primary | difficult | arbiter
    status: str  # OK | VALIDATION_FAILED | TRANSPORT_FAILED | REJECTED


@dataclass(frozen=True)
class OpenRouterRouteTrace:
    routing_version: str
    calls: tuple[RouteCallRecord, ...] = ()
    escalated: bool = False
    arbitrated: bool = False

    def to_sanitized_dict(self) -> dict:
        return {
            "routing_version": self.routing_version,
            "escalated": self.escalated,
            "arbitrated": self.arbitrated,
            "calls": [
                {"model": c.model, "role": c.role, "status": c.status} for c in self.calls
            ],
        }


class OpenRouterAIClient:
    """VietnameseGlyphAIProvider implementation with fixed OpenRouter routing."""

    model_id = "openrouter"
    model_version = ROUTING_VERSION

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY_REQUIRED")
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None
        self.last_route_trace: OpenRouterRouteTrace | None = None

    def prompt_hash(self) -> str:
        return hashlib.sha256((PROMPT_TEMPLATE + ROUTING_VERSION).encode("utf-8")).hexdigest()

    async def _http(self, model: str, prompt: str) -> str | None:
        client = self._client or httpx.AsyncClient(timeout=120.0)
        try:
            resp = await client.post(
                OPENROUTER_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])
        except Exception:
            # Sanitized: transport details never surface (may echo headers/env).
            return None
        finally:
            if self._owns_client and self._client is None:
                await client.aclose()

    @staticmethod
    def _parse_candidates(raw: str | None, missing: list[int]) -> list[AICandidateSpec] | None:
        """Strict closed-schema parse; None on any forgery/gap/non-finite content."""
        if not raw:
            return None
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            payload = json.loads(raw[start:end])
        except (ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        glyphs = payload.get("glyphs")
        if not isinstance(glyphs, list):
            return None
        specs: list[AICandidateSpec] = []
        for g in glyphs:
            try:
                contours = tuple(
                    tuple((float(x), float(y)) for x, y in contour)
                    for contour in g["contours"]
                )
                anchors = tuple(
                    (str(name), float(x), float(y)) for name, x, y in g.get("anchors", [])
                )
                spec = AICandidateSpec(
                    code_point=int(g["code_point"]),
                    contours=contours,
                    advance_width_upem=float(g["advance_width_upem"]),
                    lsb_upem=float(g["lsb_upem"]),
                    rsb_upem=float(g["rsb_upem"]),
                    ascent_upem=float(g["ascent_upem"]),
                    descent_upem=float(g["descent_upem"]),
                    anchors=anchors,
                )
            except (KeyError, ValueError, TypeError):
                return None
            try:
                spec.validate()
            except VietnameseAIIntegrityError:
                return None
            specs.append(spec)
        if {s.code_point for s in specs} != set(missing):
            return None
        return specs

    @staticmethod
    def _outline_fingerprint(specs: list[AICandidateSpec]) -> str:
        payload = [
            [s.code_point, [list(map(list, c)) for c in s.contours]] for s in sorted(specs, key=lambda s: s.code_point)
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    async def _attempt(self, model: str, request: dict) -> list[AICandidateSpec] | None:
        style_evidence = request.get("style_evidence")
        if not isinstance(style_evidence, dict) or not style_evidence:
            raise VietnameseAIIntegrityError("VI_AI_STYLE_EVIDENCE_REQUIRED")
        evidence_json = json.dumps(style_evidence, sort_keys=True, separators=(",", ":"))
        if len(evidence_json) > MAX_EVIDENCE_CHARS:
            evidence_json = evidence_json[:MAX_EVIDENCE_CHARS]
        prompt = (
            PROMPT_TEMPLATE
            .replace("__STYLE_EVIDENCE__", evidence_json)
            .replace("__UPEM__", str(request.get("units_per_em", 1000)))
            .replace("__SOURCE_HASH__", str(request.get("source_hash", "")))
            .replace("__CODE_POINTS__", json.dumps(request["missing_codepoints"]))
        )
        raw = await self._http(model, prompt)
        return self._parse_candidates(raw, list(request["missing_codepoints"]))

    @staticmethod
    def _candidate_evidence(specs: list[AICandidateSpec]) -> dict:
        """Deterministic compact candidate evidence + scores for arbitration."""
        total_points = sum(len(c) for s in specs for c in s.contours)
        areas = []
        for s in specs:
            for contour in s.contours:
                area = 0.0
                pts = list(contour)
                for i in range(len(pts)):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % len(pts)]
                    area += x0 * y1 - x1 * y0
                areas.append(abs(area) / 2.0)
        advances = [s.advance_width_upem for s in specs]
        sample = None
        if specs:
            first = sorted(specs, key=lambda s: s.code_point)[0]
            sample = {
                "code_point": first.code_point,
                "contours": [list(map(list, c)) for c in first.contours[:2]],
                "anchors": [list(a) for a in first.anchors],
            }
        return {
            "glyph_count": len(specs),
            "total_contour_points": total_points,
            "mean_advance_upem": round(sum(advances) / len(advances), 2) if advances else 0.0,
            "total_outline_area": round(sum(areas), 1) if areas else 0.0,
            "mean_outline_area": round(sum(areas) / len(areas), 1) if areas else 0.0,
            "sample_glyph": sample,
        }

    async def generate_candidates(self, request: dict) -> list[AICandidateSpec]:
        missing = list(request["missing_codepoints"])
        if not missing:
            return []
        calls: list[RouteCallRecord] = []
        escalated = False
        arbitrated = False

        # Routine: PRIMARY 12B for every case.
        primary = await self._attempt(MODEL_PRIMARY, request)
        calls.append(
            RouteCallRecord(MODEL_PRIMARY, "primary", "OK" if primary is not None else "VALIDATION_FAILED")
        )

        difficult = None
        deterministic_difficulty = len(missing) > DIFFICULT_ESCALATION_GLYPH_COUNT
        if primary is None or deterministic_difficulty:
            # Deterministic escalation to DIFFICULT 27B.
            escalated = True
            difficult = await self._attempt(MODEL_DIFFICULT, request)
            calls.append(
                RouteCallRecord(
                    MODEL_DIFFICULT, "difficult", "OK" if difficult is not None else "VALIDATION_FAILED"
                )
            )

        if primary is not None and (difficult is None or not escalated):
            self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
            return primary

        if primary is not None and difficult is not None:
            if self._outline_fingerprint(primary) == self._outline_fingerprint(difficult):
                self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
                return primary
            # Unresolved deterministic disagreement: ARBITER once.
            arbitrated = True
            arbiter_prompt = (
                ARBITER_PROMPT_TEMPLATE
                .replace("__CODE_POINTS__", json.dumps(missing))
                .replace(
                    "__EVIDENCE_A__",
                    json.dumps(self._candidate_evidence(primary), sort_keys=True)[:MAX_EVIDENCE_CHARS],
                )
                .replace(
                    "__EVIDENCE_B__",
                    json.dumps(self._candidate_evidence(difficult), sort_keys=True)[:MAX_EVIDENCE_CHARS],
                )
                .replace(
                    "__STYLE_EVIDENCE__",
                    json.dumps(request.get("style_evidence", {}), sort_keys=True)[:MAX_EVIDENCE_CHARS],
                )
            )
            raw_choice = await self._http(MODEL_ARBITER, arbiter_prompt)
            choice = None
            if raw_choice:
                try:
                    start = raw_choice.index("{")
                    end = raw_choice.rindex("}") + 1
                    choice = json.loads(raw_choice[start:end]).get("choice")
                except (ValueError, TypeError):
                    choice = None
            calls.append(
                RouteCallRecord(MODEL_ARBITER, "arbiter", "OK" if choice in ("A", "B") else "REJECTED")
            )
            if choice == "A":
                self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
                return primary
            if choice == "B":
                self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
                return difficult
            raise VietnameseAIIntegrityError("VI_AI_ARBITER_UNRESOLVED")

        if difficult is not None:
            self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
            return difficult
        if primary is not None:
            self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
            return primary

        self.last_route_trace = OpenRouterRouteTrace(ROUTING_VERSION, tuple(calls), escalated, arbitrated)
        raise VietnameseAIIntegrityError("VI_AI_ALL_ROUTES_FAILED")
