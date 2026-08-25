"""Closed provider raster-capability descriptors.

A capability descriptor is the provider-bound contract for what raster
evidence the provider can observably produce. It is bound into the
snapshot/completion/config/provenance identity: forged, unknown, or drifted
capabilities fail closed everywhere they are consumed.

Direct browser collection carries no descriptor (phase-held-out partition).
The Monotype CDN exposes the size axis only (``acs_pt``) at fixed phase
(0.0, 0.0), so its fit/held-out partition runs across distinct observable
render sizes — the provider's MAX gate, never a zero-held-out bypass.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

PROVIDER_MONOTYPE_RENDER = "monotype_render_105"

# The approved render query renders phase (0.0, 0.0) only; no parameter in
# the captured contract exposes subpixel phase control.
FIXED_PHASE: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class ProviderRasterCapability:
    """Closed descriptor of one provider's observable raster capability."""

    provider: str
    phase: tuple[float, float]
    fit_sizes: tuple[int, ...]
    held_out_sizes: tuple[int, ...]

    def validate(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("CAPABILITY_FORGED: provider identity required")
        if tuple(self.phase) != FIXED_PHASE:
            raise ValueError(
                f"CAPABILITY_FORGED: provider '{self.provider}' exposes size axis "
                f"only at fixed phase {FIXED_PHASE}, got {tuple(self.phase)}"
            )
        for sizes in (self.fit_sizes, self.held_out_sizes):
            if not sizes:
                raise ValueError("CAPABILITY_FORGED: empty size partition")
            if any(not isinstance(s, int) or s < 1 for s in sizes):
                raise ValueError("CAPABILITY_FORGED: sizes must be positive integers")
            if len(set(sizes)) != len(sizes):
                raise ValueError("CAPABILITY_FORGED: duplicate sizes in partition")
            if tuple(sorted(sizes)) != tuple(sizes):
                raise ValueError("CAPABILITY_FORGED: sizes must be sorted")
        overlap = set(self.fit_sizes) & set(self.held_out_sizes)
        if overlap:
            raise ValueError(
                f"CAPABILITY_FORGED: fit/held-out size overlap is forbidden: {sorted(overlap)}"
            )
        if len(self.fit_sizes) + len(self.held_out_sizes) < 2:
            raise ValueError(
                "CAPABILITY_FORGED: at least two distinct observable sizes are "
                "required for a disjoint size partition"
            )

    def all_sizes(self) -> tuple[int, ...]:
        return tuple(sorted(tuple(self.fit_sizes) + tuple(self.held_out_sizes)))

    def to_json(self) -> str:
        payload = {
            "provider": self.provider,
            "phase": [float(self.phase[0]), float(self.phase[1])],
            "fit_sizes": [int(s) for s in self.fit_sizes],
            "held_out_sizes": [int(s) for s in self.held_out_sizes],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def compute_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, raw: str) -> "ProviderRasterCapability":
        try:
            payload = json.loads(raw)
            capability = cls(
                provider=str(payload["provider"]),
                phase=(float(payload["phase"][0]), float(payload["phase"][1])),
                fit_sizes=tuple(int(s) for s in payload["fit_sizes"]),
                held_out_sizes=tuple(int(s) for s in payload["held_out_sizes"]),
            )
        except Exception as exc:
            raise ValueError("CAPABILITY_FORGED: malformed capability descriptor") from exc
        capability.validate()
        return capability

    @classmethod
    def deterministic_size_schedule(
        cls, provider: str, resolutions: "tuple[int, ...] | list[int]"
    ) -> "ProviderRasterCapability":
        """Deterministic disjoint allocation of observable render sizes.

        Sorted distinct sizes are alternated into fit (even indices) and
        held-out (odd indices); both partitions are non-empty and never
        overlap. Fewer than two distinct sizes fails closed.
        """
        sizes = sorted({int(r) for r in resolutions if int(r) > 0})
        if len(sizes) < 2:
            raise ValueError(
                "CAPABILITY_INSUFFICIENT_SIZES: at least two distinct observable "
                "render sizes are required for a disjoint fit/held-out partition"
            )
        capability = cls(
            provider=provider,
            phase=FIXED_PHASE,
            fit_sizes=tuple(sizes[0::2]),
            held_out_sizes=tuple(sizes[1::2]),
        )
        capability.validate()
        return capability
