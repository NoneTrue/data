# -*- coding: utf-8 -*-
"""Full-resolution residual image registration for ChangeVision 2026.

SIFT/RANSAC estimates the residual similarity transform on a reduced preview
for speed, then rasterio applies that transform to the complete raster at the
reference grid/resolution. The final output is never downsampled or tiled by
this module.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, Tuple

import cv2
import numpy as np
import rasterio
from raster_profiles import lossless_profile
from affine import Affine
from rasterio.enums import Resampling
from rasterio.warp import reproject


def _read_gray_preview(path: str, max_side: int = 3000) -> Tuple[np.ndarray, float, float]:
    with rasterio.open(path) as ds:
        out_scale = min(1.0, float(max_side) / float(max(ds.width, ds.height)))
        out_w = max(1, int(round(ds.width * out_scale)))
        out_h = max(1, int(round(ds.height * out_scale)))
        count = min(3, ds.count)
        data = ds.read(
            list(range(1, count + 1)),
            out_shape=(count, out_h, out_w),
            resampling=Resampling.average,
        )
        sx = out_w / float(ds.width)
        sy = out_h / float(ds.height)

    if count >= 3:
        img = np.moveaxis(data[:3], 0, 2)
        if img.dtype != np.uint8:
            lo, hi = np.percentile(img, (1.0, 99.0))
            img = np.clip((img.astype(np.float32) - lo) / (hi - lo + 1e-6) * 255.0, 0, 255).astype(np.uint8)
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    else:
        gray = data[0]
        if gray.dtype != np.uint8:
            lo, hi = np.percentile(gray, (1.0, 99.0))
            gray = np.clip((gray.astype(np.float32) - lo) / (hi - lo + 1e-6) * 255.0, 0, 255).astype(np.uint8)

    return np.ascontiguousarray(gray), sx, sy


def _estimate(reference: str, moving: str, max_side: int = 3000) -> Dict[str, Any]:
    a, sx_a, sy_a = _read_gray_preview(reference, max_side=max_side)
    b, sx_b, sy_b = _read_gray_preview(moving, max_side=max_side)

    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    a = a[:h, :w]
    b = b[:h, :w]

    sift = cv2.SIFT_create(nfeatures=12000, contrastThreshold=0.02, edgeThreshold=15)
    kp1, des1 = sift.detectAndCompute(a, None)
    kp2, des2 = sift.detectAndCompute(b, None)
    if des1 is None or des2 is None:
        raise RuntimeError("SIFT did not find enough features for precise alignment.")

    pairs = cv2.BFMatcher(cv2.NORM_L2).knnMatch(des1, des2, k=2)
    good = [m for m, n in pairs if m.distance < 0.70 * n.distance]
    if len(good) < 20:
        raise RuntimeError("Too few SIFT matches for reliable precise alignment: %d" % len(good))

    src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    m_small, inlier_mask = cv2.estimateAffinePartial2D(
        src,
        dst,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        maxIters=10000,
        confidence=0.999,
        refineIters=20,
    )
    if m_small is None or inlier_mask is None:
        raise RuntimeError("RANSAC could not estimate a reliable transform.")

    inliers = int(inlier_mask.ravel().sum())
    inlier_ratio = 100.0 * inliers / max(1, len(good))
    if inliers < 30 or inlier_ratio < 35.0:
        raise RuntimeError(
            "Precise alignment rejected: inliers=%d, ratio=%.2f%%" % (inliers, inlier_ratio)
        )

    # m_small maps REFERENCE preview pixels -> MOVING preview pixels.
    # Convert that mapping to full-resolution pixel coordinates:
    # M_full = S_moving^-1 * M_small * S_reference
    hm = np.eye(3, dtype=np.float64)
    hm[:2, :] = m_small
    s_ref = np.array([[sx_a, 0.0, 0.0], [0.0, sy_a, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    s_mov_inv = np.array([[1.0 / sx_b, 0.0, 0.0], [0.0, 1.0 / sy_b, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    m_full_h = s_mov_inv @ hm @ s_ref
    m_full = m_full_h[:2, :]

    dx = float(m_full[0, 2])
    dy = float(m_full[1, 2])
    rotation = math.degrees(math.atan2(float(m_full[1, 0]), float(m_full[0, 0])))
    scale = math.hypot(float(m_full[0, 0]), float(m_full[1, 0]))
    total = math.hypot(dx, dy)

    return {
        "matrix": m_full,
        "reference_keypoints": int(len(kp1)),
        "moving_keypoints": int(len(kp2)),
        "good_matches": int(len(good)),
        "inliers": inliers,
        "inlier_ratio": float(inlier_ratio),
        "x_shift_px": dx,
        "y_shift_px": dy,
        "total_shift_px": float(total),
        "rotation_deg": float(rotation),
        "scale": float(scale),
        "preview_width": int(w),
        "preview_height": int(h),
    }


def _warp_full_resolution(reference: str, moving: str, output: str, m_full: np.ndarray) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)

    # q = M*p maps reference pixel p -> moving pixel q.
    # Therefore moving pixel q should receive world position of p=M^-1*q.
    hm = np.eye(3, dtype=np.float64)
    hm[:2, :] = m_full
    inv = np.linalg.inv(hm)
    pixel_inv = Affine(
        float(inv[0, 0]), float(inv[0, 1]), float(inv[0, 2]),
        float(inv[1, 0]), float(inv[1, 1]), float(inv[1, 2]),
    )

    tmp = output + ".tmp.tif"
    if os.path.exists(tmp):
        os.remove(tmp)

    with rasterio.open(reference) as ref, rasterio.open(moving) as src:
        if ref.crs is None or src.crs is None:
            raise RuntimeError("Precise alignment requires georeferenced rasters with CRS.")

        corrected_src_transform = ref.transform * pixel_inv
        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            width=ref.width,
            height=ref.height,
            transform=ref.transform,
            crs=ref.crs,
            count=src.count,
            compress="DEFLATE",
            predictor=2 if np.dtype(src.dtypes[0]).kind in "iu" else 3,
            BIGTIFF="IF_SAFER",
        )

        with rasterio.open(tmp, "w", **lossless_profile(profile)) as dst:
            for band in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=corrected_src_transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=ref.transform,
                    dst_crs=ref.crs,
                    dst_nodata=src.nodata,
                    resampling=Resampling.bilinear,
                    num_threads=2,
                    init_dest_nodata=True,
                )

    os.replace(tmp, output)


def align_after_to_before(reference: str, moving_grid_aligned: str, output: str, max_side: int = 3000) -> Dict[str, Any]:
    """Estimate residual SIFT/RANSAC transform and apply it at full resolution."""
    before_metrics = _estimate(reference, moving_grid_aligned, max_side=max_side)
    _warp_full_resolution(reference, moving_grid_aligned, output, before_metrics["matrix"])
    after_metrics = _estimate(reference, output, max_side=max_side)

    return {
        "output": output,
        "method": "SIFT + RANSAC + full-resolution geospatial warp",
        "full_resolution": True,
        "no_output_downsampling": True,
        "before": {k: v for k, v in before_metrics.items() if k != "matrix"},
        "after": {k: v for k, v in after_metrics.items() if k != "matrix"},
    }
