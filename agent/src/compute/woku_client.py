"""Woku-primary bounded cascade for the Vietnamese AI provider contract.

Fixed routing (exact models, no substitution, bounded, fail-closed):
1. PRIMARY   exact model ``gpt-5.6-luna``   at Woku base https://llm.wokushop.com/v1
2. FALLBACK  exact model ``gemini-3.7-flash`` at the same Woku base, exactly once,
             only when PRIMARY is unavailable/invalid/deterministically rejected.
3. DOWNSTREAM if both Woku attempts fail, the existing fixed OpenRouter route
             (unchanged) is invoked at most once as the downstream fallback.

Each Woku model is attempted at most once per generation request: no retry
loops, no model substitution, no schema/validator bypass. On every attempt, the
serving model identity returned by the Woku endpoint is verified against the
exact requested model. Any missing, unverifiable, or substituted model is
deterministically rejected as INVALID and falls through to the next stage.
The identical closed prompt schema and candidate parser of the OpenRouter route
are reused, so every candidate still passes the exact deterministic validators.

Runtime secret only (key name ``wokushop_api_key`` at the composition
boundary); sanitized provenance: provider/model/served_model/route/fallback-reason/call
statuses are recorded without secret values or raw transport details.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import httpx

from compute.openrouter_client import (
    MAX_EVIDENCE_CHARS,
    PROMPT_TEMPLATE,
    OpenRouterAIClient,
)
from compute.vietnamese import AICandidateSpec, VietnameseAIIntegrityError

WOKU_BASE_URL = "https://llm.wokushop.com/v1"
WOKU_CHAT_ENDPOINT = f"{WOKU_BASE_URL}/chat/completions"
WOKU_MODEL_PRIMARY = "gpt-5.6-luna"
WOKU_MODEL_FALLBACK = "gemini-3.7-flash"
WOKU_ROUTING_VERSION = "woku-cascade-v1"

# Bounded sanitized fallback reasons (fixed vocabulary; bound into provenance).
REASON_UNAVAILABLE = "unavailable"
REASON_INVALID = "invalid"
REASON_MODEL_MISMATCH = "model_mismatch"
REASON_MODEL_UNVERIFIABLE = "model_unverifiable"

ROUTE_WOKU_PRIMARY = "woku-primary"
ROUTE_WOKU_FALLBACK = "woku-fallback"
ROUTE_OPENROUTER = "openrouter-route"


def _sanitize_model_name(val: Any) -> str:
    """Sanitize model identifier string: printable ASCII/safe chars, bounded length."""
    if not isinstance(val, str):
        return ""
    cleaned = "".join(c for c in val if c.isprintable() and not c.isspace())
    return cleaned[:100]


@dataclass(frozen=True)
class CascadeCallRecord:
    """Sanitized provenance for one cascade call (never includes secrets)."""

    provider: str  # woku | openrouter
    model: str  # requested model
    role: str  # woku-primary | woku-fallback | downstream-route
    status: str  # OK | UNAVAILABLE | INVALID
    served_model: str = ""  # authoritative echoed model from endpoint (sanitized)


@dataclass(frozen=True)
class CascadeRouteTrace:
    routing_version: str
    route: str  # woku-primary | woku-fallback | openrouter-route
    fallback_reason: str  # "" when the Woku PRIMARY succeeded
    calls: tuple[CascadeCallRecord, ...] = ()

    def to_sanitized_dict(self) -> dict:
        return {
            "routing_version": self.routing_version,
            "route": self.route,
            "fallback_reason": self.fallback_reason,
            "calls": [
                {
                    "provider": c.provider,
                    "model": c.model,
                    "role": c.role,
                    "status": c.status,
                    "served_model": c.served_model,
                }
                for c in self.calls
            ],
        }


class WokuCascadeAIClient:
    """VietnameseGlyphAIProvider: Woku-primary cascade with bounded fallback.

    ``downstream`` is the unchanged existing OpenRouter route client; it is
    only contacted when both exact Woku models fail. Every stage is bounded
    and fail-closed: an all-routes failure raises ``VI_AI_ALL_ROUTES_FAILED``.
    """

    model_id = "woku-cascade"
    model_version = WOKU_ROUTING_VERSION

    def __init__(
        self,
        api_key: str,
        downstream: OpenRouterAIClient | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("WOKUSHOP_API_KEY_REQUIRED")
        self._api_key = api_key
        self._downstream = downstream
        self._client = client
        self._owns_client = client is None
        self.last_route_trace: CascadeRouteTrace | None = None

    def prompt_hash(self) -> str:
        identity = (
            PROMPT_TEMPLATE
            + WOKU_ROUTING_VERSION
            + WOKU_MODEL_PRIMARY
            + WOKU_MODEL_FALLBACK
        )
        if self._downstream is not None:
            identity += self._downstream.prompt_hash()
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    async def _http(self, model: str, prompt: str) -> tuple[str | None, str, str]:
        """Perform HTTP call to Woku chat completions endpoint.

        Returns (raw_content, served_model, reason_if_failed):
        - On success: (content_str, verified_served_model, "")
        - On transport / non-200 failure: (None, "", REASON_UNAVAILABLE)
        - On missing/unverifiable model identity: (None, "", REASON_MODEL_UNVERIFIABLE)
        - On model mismatch (substitution): (None, sanitized_served_model, REASON_MODEL_MISMATCH)
        - On malformed payload: (None, served_model, REASON_INVALID)
        """
        client = self._client or httpx.AsyncClient(timeout=120.0)
        try:
            resp = await client.post(
                WOKU_CHAT_ENDPOINT,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                return None, "", REASON_UNAVAILABLE
            try:
                data = resp.json()
            except Exception:
                return None, "", REASON_INVALID
            if not isinstance(data, dict):
                return None, "", REASON_INVALID

            raw_served_model = data.get("model")
            if not isinstance(raw_served_model, str) or not raw_served_model.strip():
                return None, "", REASON_MODEL_UNVERIFIABLE

            served_model = _sanitize_model_name(raw_served_model)
            if served_model != model:
                # Structural enforcement: no model substitution permitted.
                return None, served_model, REASON_MODEL_MISMATCH

            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return None, served_model, REASON_INVALID
            first_choice = choices[0]
            if not isinstance(first_choice, dict):
                return None, served_model, REASON_INVALID
            message = first_choice.get("message")
            if not isinstance(message, dict):
                return None, served_model, REASON_INVALID
            content = message.get("content")
            if content is None:
                return None, served_model, REASON_INVALID
            return str(content), served_model, ""
        except Exception:
            # Sanitized: transport details never surface (may echo headers/env).
            return None, "", REASON_UNAVAILABLE
        finally:
            if self._owns_client and self._client is None:
                await client.aclose()

    def _build_prompt(self, request: dict) -> str:
        style_evidence = request.get("style_evidence")
        if not isinstance(style_evidence, dict) or not style_evidence:
            raise VietnameseAIIntegrityError("VI_AI_STYLE_EVIDENCE_REQUIRED")
        evidence_json = json.dumps(style_evidence, sort_keys=True, separators=(",", ":"))
        if len(evidence_json) > MAX_EVIDENCE_CHARS:
            evidence_json = evidence_json[:MAX_EVIDENCE_CHARS]
        return (
            PROMPT_TEMPLATE
            .replace("__STYLE_EVIDENCE__", evidence_json)
            .replace("__UPEM__", str(request.get("units_per_em", 1000)))
            .replace("__SOURCE_HASH__", str(request.get("source_hash", "")))
            .replace("__CODE_POINTS__", json.dumps(request["missing_codepoints"]))
        )

    async def _woku_attempt(
        self, model: str, prompt: str, missing: list[int]
    ) -> tuple[list[AICandidateSpec] | None, str, str]:
        """Exactly one bounded attempt against one exact Woku model.

        Returns (specs, "", served_model) on success or (None, sanitized_reason, served_model):
        the serving model identity echoed by the endpoint must exactly match
        the requested model; the closed schema/validator is never bypassed.
        """
        raw_content, served_model, http_reason = await self._http(model, prompt)
        if http_reason != "":
            return None, http_reason, served_model
        if raw_content is None:
            return None, REASON_INVALID, served_model
        specs = OpenRouterAIClient._parse_candidates(raw_content, missing)
        if specs is None:
            return None, REASON_INVALID, served_model
        return specs, "", served_model

    async def generate_candidates(self, request: dict) -> list[AICandidateSpec]:
        missing = list(request["missing_codepoints"])
        if not missing:
            return []

        calls: list[CascadeCallRecord] = []
        prompt = self._build_prompt(request)

        # Stage 1: exact Woku PRIMARY, one bounded attempt.
        specs, reason, served1 = await self._woku_attempt(WOKU_MODEL_PRIMARY, prompt, missing)
        calls.append(
            CascadeCallRecord(
                "woku",
                WOKU_MODEL_PRIMARY,
                "woku-primary",
                "OK" if specs else ("UNAVAILABLE" if reason == REASON_UNAVAILABLE else "INVALID"),
                served_model=served1,
            )
        )
        if specs is not None:
            self.last_route_trace = CascadeRouteTrace(
                WOKU_ROUTING_VERSION, ROUTE_WOKU_PRIMARY, "", tuple(calls)
            )
            return specs

        fallback_reason = f"woku_primary_{reason}"

        # Stage 2: exact Woku FALLBACK, one bounded attempt.
        specs2, reason2, served2 = await self._woku_attempt(WOKU_MODEL_FALLBACK, prompt, missing)
        calls.append(
            CascadeCallRecord(
                "woku",
                WOKU_MODEL_FALLBACK,
                "woku-fallback",
                "OK" if specs2 else ("UNAVAILABLE" if reason2 == REASON_UNAVAILABLE else "INVALID"),
                served_model=served2,
            )
        )
        if specs2 is not None:
            self.last_route_trace = CascadeRouteTrace(
                WOKU_ROUTING_VERSION, ROUTE_WOKU_FALLBACK, fallback_reason, tuple(calls)
            )
            return specs2

        fallback_reason += f"+woku_fallback_{reason2}"

        # Stage 3: existing OpenRouter route unchanged as downstream fallback.
        if self._downstream is None:
            self.last_route_trace = CascadeRouteTrace(
                WOKU_ROUTING_VERSION, ROUTE_OPENROUTER, fallback_reason, tuple(calls)
            )
            raise VietnameseAIIntegrityError("VI_AI_ALL_ROUTES_FAILED")

        try:
            specs3 = await self._downstream.generate_candidates(request)
        except VietnameseAIIntegrityError:
            downstream_trace = getattr(self._downstream, "last_route_trace", None)
            downstream_calls = tuple(
                CascadeCallRecord(
                    "openrouter",
                    c.model,
                    "downstream-route",
                    c.status,
                    served_model=getattr(c, "served_model", c.model),
                )
                for c in getattr(downstream_trace, "calls", ())
            )
            self.last_route_trace = CascadeRouteTrace(
                WOKU_ROUTING_VERSION,
                ROUTE_OPENROUTER,
                fallback_reason,
                tuple(calls) + downstream_calls,
            )
            raise
        downstream_trace = getattr(self._downstream, "last_route_trace", None)
        downstream_calls = tuple(
            CascadeCallRecord(
                "openrouter",
                c.model,
                "downstream-route",
                c.status,
                served_model=getattr(c, "served_model", c.model),
            )
            for c in getattr(downstream_trace, "calls", ())
        )
        calls.extend(downstream_calls)
        self.last_route_trace = CascadeRouteTrace(
            WOKU_ROUTING_VERSION, ROUTE_OPENROUTER, fallback_reason, tuple(calls)
        )
        return specs3

    def route_model_identities(self) -> tuple[str, ...]:
        """Exact model identities attempted/succeeded, in order (sanitized)."""
        trace = self.last_route_trace
        if trace is None:
            return ()
        return tuple(c.model for c in trace.calls)