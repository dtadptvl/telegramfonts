"""Continuous Signed Distance Field (SDF) computation and multi-observation fusion."""
from __future__ import annotations

import io
import math
import numpy as np
import scipy.ndimage as ndi
from PIL import Image

from measurement.models import ObservationRecord
from reconstruction.models import ReconstructionConfig


def compute_observation_sdf(png_bytes: bytes) -> np.ndarray:
    """Compute subpixel-refined signed distance field in pixel units from lossless PNG bytes.
    
    Returns:
        2D numpy array where positive values are inside ink, negative outside, zero at boundary.
    """
    if not png_bytes:
        return np.zeros((128, 128), dtype=np.float32)

    img = Image.open(io.BytesIO(png_bytes)).convert("L")
    arr = np.array(img, dtype=np.float32) / 255.0
    # Inverted: 1.0 is black ink, 0.0 is white background
    ink = 1.0 - arr

    # Binary mask for distance transform
    mask = ink >= 0.5

    if not np.any(mask):
        # Empty glyph (e.g. space)
        return -np.ones_like(ink, dtype=np.float32) * 100.0

    # Euclidean distance transforms
    d_in = ndi.distance_transform_edt(mask)
    d_out = ndi.distance_transform_edt(~mask)

    # Base signed distance in pixels (positive inside, negative outside)
    sdf = d_in - d_out

    # Subpixel anti-aliasing correction near boundary (ink coverage in [0.05, 0.95])
    boundary_mask = (ink > 0.05) & (ink < 0.95)
    # Adjust zero crossing by fractional pixel coverage
    sdf[boundary_mask] = (ink[boundary_mask] - 0.5)

    return sdf.astype(np.float32)


def fuse_observation_sdfs(
    observations: list[tuple[ObservationRecord, bytes]],
    config: ReconstructionConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[float, float, float, float]]:
    """Transform and fuse multi-resolution / subpixel observations into a canonical continuous UPEM SDF grid.
    
    Returns:
        (fused_sdf_grid, x_coords_1d, y_coords_1d, bounding_box_upem)
    """
    if not observations:
        # Fallback empty grid
        x = np.linspace(0, 1000, config.grid_resolution, dtype=np.float32)
        y = np.linspace(-200, 800, config.grid_resolution, dtype=np.float32)
        return -np.ones((config.grid_resolution, config.grid_resolution), dtype=np.float32), x, y, (0.0, -200.0, 1000.0, 800.0)

    # Canonical design space bounds
    m = observations[0][0].metrics
    x_min = float(m.lsb_upem)
    x_max = float(m.lsb_upem + max(m.bbox_width_upem, m.advance_width_upem * 0.8))
    y_min = float(-m.descent_upem)
    y_max = float(m.ascent_upem)

    # Ensure valid positive bounds
    if x_max <= x_min:
        x_max = x_min + 500.0
    if y_max <= y_min:
        y_max = y_min + 1000.0

    x_coords = np.linspace(x_min - config.sdf_pad_upem, x_max + config.sdf_pad_upem, config.grid_resolution, dtype=np.float32)
    y_coords = np.linspace(y_min - config.sdf_pad_upem, y_max + config.sdf_pad_upem, config.grid_resolution, dtype=np.float32)

    X_grid, Y_grid = np.meshgrid(x_coords, y_coords)
    fused_sdf = np.zeros((config.grid_resolution, config.grid_resolution), dtype=np.float32)
    total_weight = 0.0

    # Prioritize highest resolution observations (e.g. 256px) if available
    max_res = max((rec.resolution for rec, _ in observations), default=128)
    active_obs = [(rec, b) for rec, b in observations if rec.resolution == max_res]
    if not active_obs:
        active_obs = observations

    for rec, png_bytes in active_obs:
        if not png_bytes:
            continue

        raw_sdf = compute_observation_sdf(png_bytes)
        res = rec.resolution
        f_size = math.floor(res * 0.72)
        scale = f_size / 1000.0

        # Exact canvas coordinate metrics
        raw_ascent_px = rec.metrics.raw_actual_ascent * (f_size / 200.0)
        raw_descent_px = (-rec.metrics.raw_actual_descent) * (f_size / 200.0)
        ascent_px = raw_ascent_px if raw_ascent_px > 0.001 else (f_size * 0.72)
        descent_px = raw_descent_px if raw_descent_px > 0.001 else (f_size * 0.2)
        total_h_px = ascent_px + descent_px
        adv_px = rec.metrics.raw_advance_width * (f_size / 200.0)

        x_base = round((res - adv_px) / 2.0) + rec.subpixel_x
        y_base = round((res - total_h_px) / 2.0 + ascent_px) + rec.subpixel_y

        U_pixel = x_base + X_grid * scale
        V_pixel = y_base - Y_grid * scale

        sampled_pixel_sdf = ndi.map_coordinates(
            raw_sdf,
            [V_pixel, U_pixel],
            order=1,
            mode="nearest",
        )
        sampled_upem_sdf = sampled_pixel_sdf / max(scale, 1e-6)

        weight = float(res / 256.0)
        fused_sdf += sampled_upem_sdf * weight
        total_weight += weight

    if total_weight > 0:
        fused_sdf /= total_weight
    else:
        fused_sdf = -np.ones((config.grid_resolution, config.grid_resolution), dtype=np.float32)

    bbox_upem = (float(x_min), float(y_min), float(x_max), float(y_max))
    return fused_sdf, x_coords, y_coords, bbox_upem
