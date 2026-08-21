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
    x_max = float(m.lsb_upem + max(m.bbox_width_upem, 10.0))
    y_min = float(m.descent_upem)
    y_max = float(max(m.ascent_upem, y_min + 10.0))

    x_coords = np.linspace(x_min - config.sdf_pad_upem, x_max + config.sdf_pad_upem, config.grid_resolution, dtype=np.float32)
    y_coords = np.linspace(y_min - config.sdf_pad_upem, y_max + config.sdf_pad_upem, config.grid_resolution, dtype=np.float32)

    X_grid, Y_grid = np.meshgrid(x_coords, y_coords)
    fused_sdf = np.zeros((config.grid_resolution, config.grid_resolution), dtype=np.float32)
    total_weight = 0.0

    for rec, png_bytes in observations:
        if not png_bytes:
            continue

        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        arr = 1.0 - np.array(img, dtype=np.float32) / 255.0
        mask = arr >= 0.5
        if not np.any(mask):
            continue

        v_idx, u_idx = np.where(mask)
        u_min_obs, u_max_obs = float(u_idx.min()), float(u_idx.max())
        v_min_obs, v_max_obs = float(v_idx.min()), float(v_idx.max())
        if u_max_obs <= u_min_obs or v_max_obs <= v_min_obs:
            continue

        d_in = ndi.distance_transform_edt(mask)
        d_out = ndi.distance_transform_edt(~mask)
        sdf_raw = d_in - d_out

        # Subpixel anti-aliasing boundary refinement
        boundary = (arr > 0.05) & (arr < 0.95)
        sdf_raw[boundary] = arr[boundary] - 0.5

        # Metric affine mapping to raster pixel coordinates
        scale_u = (u_max_obs - u_min_obs) / max(x_max - x_min, 1.0)
        scale_v = (v_max_obs - v_min_obs) / max(y_max - y_min, 1.0)

        U_map = u_min_obs + (X_grid - x_min) * scale_u
        V_map = v_max_obs - (Y_grid - y_min) * scale_v

        sampled_pixel_sdf = ndi.map_coordinates(
            sdf_raw,
            [V_map, U_map],
            order=1,
            mode="nearest",
        )
        avg_scale = 0.5 * (scale_u + scale_v)
        sampled_upem_sdf = sampled_pixel_sdf / max(avg_scale, 1e-6)

        # Quadratic resolution weighting (256px has 4x weight of 128px)
        weight = float((rec.resolution / 128.0) ** 2)
        fused_sdf += sampled_upem_sdf * weight
        total_weight += weight

    if total_weight > 0:
        fused_sdf /= total_weight
    else:
        fused_sdf = -np.ones((config.grid_resolution, config.grid_resolution), dtype=np.float32)

    bbox_upem = (float(x_min), float(y_min), float(x_max), float(y_max))
    return fused_sdf, x_coords, y_coords, bbox_upem
