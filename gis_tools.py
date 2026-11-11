from __future__ import annotations

import os
import tempfile
from typing import Dict, Tuple

import numpy as np
import rasterio
from raster_profiles import lossless_profile
from rasterio.windows import Window, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds


def _crs_text(crs):
    if crs is None:
        return None
    try:
        epsg = crs.to_epsg()
        return f"EPSG:{epsg}" if epsg else crs.to_string()
    except Exception:
        return str(crs)


def _copy_band_metadata(src, dst):
    """Keep explicit band semantics when creating a new grid or crop."""
    dst.colorinterp = src.colorinterp
    excluded = {'crs','geotransform','transform','extent','bounds','origin','pixel_size','rpc','gcps'}
    dst.update_tags(**{k:v for k,v in src.tags().items() if k.lower() not in excluded})
    dst.scales = src.scales
    dst.offsets = src.offsets
    for i in range(1, src.count + 1):
        dst.update_tags(i, **src.tags(i))
        if src.descriptions[i-1]:
            dst.set_band_description(i, src.descriptions[i-1])
        if src.units[i-1]:
            dst.set_band_unit(i, src.units[i-1])
        bits = src.tags(i, ns='IMAGE_STRUCTURE').get('NBITS') or src.tags(ns='IMAGE_STRUCTURE').get('NBITS')
        if bits:
            dst.update_tags(i, NBITS=bits)


def raster_info(path: str) -> Dict:
    path = os.fspath(path)
    with rasterio.open(path) as src:
        b = src.bounds
        return {
            "path": path,
            "driver": src.driver,
            "width": int(src.width),
            "height": int(src.height),
            "bands": int(src.count),
            "dtypes": list(src.dtypes),
            "crs": _crs_text(src.crs),
            "crs_wkt": src.crs.to_wkt() if src.crs else None,
            "pixel_size_x": float(src.res[0]),
            "pixel_size_y": float(src.res[1]),
            "bounds": {
                "left": float(b.left),
                "bottom": float(b.bottom),
                "right": float(b.right),
                "top": float(b.top),
            },
            "nodata": src.nodata,
        }


def _bounds_in_crs(src, target_crs) -> Tuple[float, float, float, float]:
    b = src.bounds
    if src.crs is None or target_crs is None:
        if src.crs != target_crs:
            raise RuntimeError("لا يمكن حساب التداخل لأن إحدى الصورتين لا تحتوي CRS صالحًا.")
        return float(b.left), float(b.bottom), float(b.right), float(b.top)
    if src.crs == target_crs:
        return float(b.left), float(b.bottom), float(b.right), float(b.top)
    return tuple(float(v) for v in transform_bounds(src.crs, target_crs, *b, densify_pts=21))


def inspect_pair(path_a: str, path_b: str) -> Dict:
    info_a = raster_info(path_a)
    info_b = raster_info(path_b)
    with rasterio.open(path_a) as a, rasterio.open(path_b) as b:
        if a.crs is None or b.crs is None:
            overlap = None
        else:
            ab = (float(a.bounds.left), float(a.bounds.bottom), float(a.bounds.right), float(a.bounds.top))
            bb = _bounds_in_crs(b, a.crs)
            left = max(ab[0], bb[0])
            bottom = max(ab[1], bb[1])
            right = min(ab[2], bb[2])
            top = min(ab[3], bb[3])
            if right <= left or top <= bottom:
                overlap = {
                    "exists": False,
                    "percent_a": 0.0,
                    "percent_b": 0.0,
                    "bounds_in_a_crs": None,
                }
            else:
                area_o = (right-left) * (top-bottom)
                area_a = max(1e-30, (ab[2]-ab[0]) * (ab[3]-ab[1]))
                area_b = max(1e-30, (bb[2]-bb[0]) * (bb[3]-bb[1]))
                overlap = {
                    "exists": True,
                    "percent_a": 100.0 * area_o / area_a,
                    "percent_b": 100.0 * area_o / area_b,
                    "bounds_in_a_crs": [left, bottom, right, top],
                }
    return {"image1": info_a, "image2": info_b, "overlap": overlap}


def format_pair_report(info: Dict) -> str:
    a = info.get("image1", {})
    b = info.get("image2", {})
    ov = info.get("overlap")
    lines = [
        "ChangeVision 2026 — GIS Preflight",
        "=" * 62,
        f"Image 1: {a.get('width')} × {a.get('height')} | bands={a.get('bands')} | CRS={a.get('crs')}",
        f"Pixel 1: {a.get('pixel_size_x')} × {a.get('pixel_size_y')}",
        f"Image 2: {b.get('width')} × {b.get('height')} | bands={b.get('bands')} | CRS={b.get('crs')}",
        f"Pixel 2: {b.get('pixel_size_x')} × {b.get('pixel_size_y')}",
        f"Extent 1: {a.get('bounds')}",
        f"Extent 2: {b.get('bounds')}",
    ]
    if ov is None:
        lines.append("Overlap: غير متاح بسبب CRS مفقود.")
    elif not ov.get("exists"):
        lines.append("Overlap: لا يوجد تداخل جغرافي.")
    else:
        lines.append(f"Overlap image 1: {ov.get('percent_a', 0):.2f}%")
        lines.append(f"Overlap image 2: {ov.get('percent_b', 0):.2f}%")
        lines.append("Overlap bounds: " + ", ".join(f"{x:.10f}" for x in ov["bounds_in_a_crs"]))
    return "\n".join(lines)


def _clip_window(src, bounds):
    win = from_bounds(*bounds, transform=src.transform)
    win = win.round_offsets().round_lengths()
    full = Window(0, 0, src.width, src.height)
    win = win.intersection(full)
    if win.width <= 0 or win.height <= 0:
        raise RuntimeError("منطقة التداخل لا تحتوي بكسلات صالحة.")
    return win


def _write_window(src, window, output: str):
    if os.path.realpath(src.name).casefold()==os.path.realpath(output).casefold():
        raise ValueError('لا يمكن القص فوق ملف المصدر.')
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    profile = src.profile.copy()
    profile.update(
        width=int(window.width),
        height=int(window.height),
        transform=src.window_transform(window),
        driver="GTiff",
        BIGTIFF="IF_SAFER",
        compress="deflate",
    )
    profile = lossless_profile(profile)
    with rasterio.open(output, "w", **profile) as dst:
        _copy_band_metadata(src, dst)
        for band in range(1, src.count + 1):
            dst.write(src.read(band, window=window), band)
        dst.write_mask(src.dataset_mask(window=window))
    return output


def crop_overlap(path_a: str, path_b: str, out_a: str, out_b: str) -> Dict:
    inputs={os.path.realpath(p).casefold() for p in (path_a,path_b)}
    outputs=[os.path.realpath(p).casefold() for p in (out_a,out_b)]
    if any(p in inputs for p in outputs) or outputs[0]==outputs[1]:
        raise ValueError('يجب حفظ القص في ملفين مختلفين عن الأصول.')
    with rasterio.open(path_a) as a, rasterio.open(path_b) as b:
        if a.crs is None or b.crs is None:
            raise RuntimeError("قص التداخل يحتاج CRS صالحًا في الصورتين.")
        ab = (float(a.bounds.left), float(a.bounds.bottom), float(a.bounds.right), float(a.bounds.top))
        bb_a = _bounds_in_crs(b, a.crs)
        overlap_a = (
            max(ab[0], bb_a[0]),
            max(ab[1], bb_a[1]),
            min(ab[2], bb_a[2]),
            min(ab[3], bb_a[3]),
        )
        if overlap_a[2] <= overlap_a[0] or overlap_a[3] <= overlap_a[1]:
            raise RuntimeError("لا يوجد تداخل جغرافي بين الصورتين.")
        win_a = _clip_window(a, overlap_a)
        overlap_b = transform_bounds(a.crs, b.crs, *overlap_a, densify_pts=21)
        win_b = _clip_window(b, overlap_b)
        _write_window(a, win_a, out_a)
        _write_window(b, win_b, out_b)
        return {
            "before_overlap": out_a,
            "after_overlap": out_b,
            "before_size": [int(win_a.width), int(win_a.height)],
            "after_size": [int(win_b.width), int(win_b.height)],
            "full_resolution": True,
        }


def _aligned_output_profile(src, ref):
    """Create a fast tiled GeoTIFF profile while preserving the source data type/bands."""
    profile = src.profile.copy()
    profile.update(
        driver="GTiff",
        crs=ref.crs,
        transform=ref.transform,
        width=ref.width,
        height=ref.height,
        BIGTIFF="IF_SAFER",
        tiled=True,
        blockxsize=512,
        blockysize=512,
        compress="deflate",
        zlevel=1,
        NUM_THREADS="ALL_CPUS",
    )
    return lossless_profile(profile)


def _align_cpu(ref, src, output: str, method, cancel=None) -> Dict:
    """Fast CPU path. GDAL streams the raster in blocks and uses all CPU cores."""
    cpu_count = max(1, os.cpu_count() or 1)
    profile = _aligned_output_profile(src, ref)
    with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS", GDAL_CACHEMAX=512 * 1024 * 1024):
        with rasterio.open(output, "w", **lossless_profile(profile)) as dst:
            _copy_band_metadata(src, dst)
            for band in range(1, src.count + 1):
                from preparation import check_cancel
                check_cancel(cancel)
                reproject(
                    source=rasterio.band(src, band),
                    destination=rasterio.band(dst, band),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=ref.transform,
                    dst_crs=ref.crs,
                    dst_nodata=src.nodata,
                    resampling=method,
                    num_threads=cpu_count,
                    warp_mem_limit=512,
                )
    return {"processor_used": "CPU", "cpu_threads": cpu_count, "tile_size": 512}


def _gpu_imports():
    """Load CuPy only when the user explicitly asks for GPU processing."""
    try:
        import cupy as cp
        from cupyx.scipy.ndimage import map_coordinates
    except Exception as exc:
        raise RuntimeError(
            "وضع GPU يحتاج CuPy المتوافق مع NVIDIA CUDA. "
            "ثبّت الحزمة المناسبة، مثال: pip install cupy-cuda12x"
        ) from exc
    try:
        if int(cp.cuda.runtime.getDeviceCount()) < 1:
            raise RuntimeError("لم يتم العثور على NVIDIA GPU متاح لـ CUDA.")
    except Exception as exc:
        raise RuntimeError("تعذر تشغيل CUDA على كرت NVIDIA. تحقق من تعريف NVIDIA/CUDA.") from exc
    return cp, map_coordinates


def _cast_gpu_result(cp, arr, dtype):
    dtype = np.dtype(dtype)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        arr = cp.rint(arr)
        arr = cp.clip(arr, info.min, info.max)
    return arr.astype(dtype, copy=False)


def _gpu_sample_band(cp, map_coordinates, band_gpu, row_gpu, col_gpu, order, nodata, out_dtype):
    """Sample one band on the GPU and respect nodata as far as possible."""
    if nodata is None:
        sampled = map_coordinates(band_gpu, (row_gpu, col_gpu), order=order, mode="constant", cval=0.0)
        return _cast_gpu_result(cp, sampled, out_dtype)

    if isinstance(nodata, float) and np.isnan(nodata):
        valid = cp.isfinite(band_gpu)
        fill_value = cp.float32(0.0)
    else:
        valid = band_gpu != nodata
        fill_value = cp.float32(nodata)

    if order == 0:
        sampled = map_coordinates(band_gpu, (row_gpu, col_gpu), order=0, mode="constant", cval=float(fill_value))
        return _cast_gpu_result(cp, sampled, out_dtype)

    # Weighted interpolation prevents nodata pixels from contaminating neighboring valid pixels.
    data = cp.where(valid, band_gpu, 0.0).astype(cp.float32, copy=False)
    weights = valid.astype(cp.float32, copy=False)
    sampled_data = map_coordinates(data, (row_gpu, col_gpu), order=order, mode="constant", cval=0.0)
    sampled_w = map_coordinates(weights, (row_gpu, col_gpu), order=order, mode="constant", cval=0.0)
    sampled = cp.where(sampled_w > 1e-6, sampled_data / cp.maximum(sampled_w, 1e-6), fill_value)
    return _cast_gpu_result(cp, sampled, out_dtype)


def _align_gpu(ref, src, output: str, method, resampling_name: str, cancel=None) -> Dict:
    """
    GPU path using CuPy. The raster is processed tile-by-tile, so the full image
    is never loaded into GPU RAM. Geographic metadata stays exactly on the
    reference grid. CRS coordinate transformation remains on CPU when the two
    CRSs differ; interpolation/resampling runs on the NVIDIA GPU.
    """
    cp, map_coordinates = _gpu_imports()
    from affine import Affine
    from pyproj import Transformer
    from rasterio.windows import Window

    order_map = {"nearest": 0, "bilinear": 1, "cubic": 3}
    order = order_map.get(str(resampling_name).lower(), 1)
    tile = 1024
    margin = 4 if order >= 3 else 2
    profile = _aligned_output_profile(src, ref)
    same_crs = bool(src.crs == ref.crs)
    transformer = None if same_crs else Transformer.from_crs(ref.crs, src.crs, always_xy=True)
    inv_src = ~src.transform
    composite = inv_src * ref.transform if same_crs else None
    nodata = src.nodata

    # Get GPU name before starting.
    try:
        props = cp.cuda.runtime.getDeviceProperties(cp.cuda.Device().id)
        raw_name = props.get("name", b"NVIDIA GPU") if isinstance(props, dict) else b"NVIDIA GPU"
        gpu_name = raw_name.decode(errors="ignore") if isinstance(raw_name, (bytes, bytearray)) else str(raw_name)
    except Exception:
        gpu_name = "NVIDIA GPU"

    with rasterio.Env(GDAL_CACHEMAX=512 * 1024 * 1024):
        with rasterio.open(output, "w", **lossless_profile(profile)) as dst:
            _copy_band_metadata(src, dst)
            for y0 in range(0, ref.height, tile):
                from preparation import check_cancel
                check_cancel(cancel)
                h = min(tile, ref.height - y0)
                for x0 in range(0, ref.width, tile):
                    w = min(tile, ref.width - x0)

                    if same_crs:
                        # Composite affine maps destination pixel-corner coordinates
                        # directly to source pixel-corner coordinates.
                        corners = []
                        for rr, cc in ((y0 + 0.5, x0 + 0.5),
                                       (y0 + 0.5, x0 + w - 0.5),
                                       (y0 + h - 0.5, x0 + 0.5),
                                       (y0 + h - 0.5, x0 + w - 0.5)):
                            sc, sr = composite * (cc, rr)
                            corners.append((sc - 0.5, sr - 0.5))
                        min_c = min(v[0] for v in corners); max_c = max(v[0] for v in corners)
                        min_r = min(v[1] for v in corners); max_r = max(v[1] for v in corners)
                        mapping_cpu = None
                    else:
                        rows = np.arange(y0, y0 + h, dtype=np.float64) + 0.5
                        cols = np.arange(x0, x0 + w, dtype=np.float64) + 0.5
                        cc, rr = np.meshgrid(cols, rows)
                        xw = ref.transform.a * cc + ref.transform.b * rr + ref.transform.c
                        yw = ref.transform.d * cc + ref.transform.e * rr + ref.transform.f
                        sx, sy = transformer.transform(xw, yw)
                        src_c = inv_src.a * sx + inv_src.b * sy + inv_src.c - 0.5
                        src_r = inv_src.d * sx + inv_src.e * sy + inv_src.f - 0.5
                        finite = np.isfinite(src_c) & np.isfinite(src_r)
                        if not np.any(finite):
                            fill = 0 if nodata is None else nodata
                            empty = np.full((src.count, h, w), fill, dtype=src.dtypes[0])
                            dst.write(empty, window=Window(x0, y0, w, h))
                            continue
                        min_c = float(np.nanmin(src_c[finite])); max_c = float(np.nanmax(src_c[finite]))
                        min_r = float(np.nanmin(src_r[finite])); max_r = float(np.nanmax(src_r[finite]))
                        mapping_cpu = (src_r, src_c, finite)

                    sx0 = max(0, int(np.floor(min_c)) - margin)
                    sy0 = max(0, int(np.floor(min_r)) - margin)
                    sx1 = min(src.width, int(np.ceil(max_c)) + margin + 1)
                    sy1 = min(src.height, int(np.ceil(max_r)) + margin + 1)

                    fill = 0 if nodata is None else nodata
                    if sx1 <= sx0 or sy1 <= sy0:
                        empty = np.full((src.count, h, w), fill, dtype=src.dtypes[0])
                        dst.write(empty, window=Window(x0, y0, w, h))
                        continue

                    src_window = Window(sx0, sy0, sx1 - sx0, sy1 - sy0)
                    src_np = src.read(window=src_window)
                    src_gpu = cp.asarray(src_np, dtype=cp.float32)

                    if same_crs:
                        r = cp.arange(y0, y0 + h, dtype=cp.float32) + cp.float32(0.5)
                        c = cp.arange(x0, x0 + w, dtype=cp.float32) + cp.float32(0.5)
                        ccg = c[None, :]
                        rrg = r[:, None]
                        src_col = (composite.a * ccg + composite.b * rrg + composite.c - 0.5) - sx0
                        src_row = (composite.d * ccg + composite.e * rrg + composite.f - 0.5) - sy0
                    else:
                        src_r, src_c, finite = mapping_cpu
                        src_row = cp.asarray(src_r - sy0, dtype=cp.float32)
                        src_col = cp.asarray(src_c - sx0, dtype=cp.float32)
                        finite_gpu = cp.asarray(finite)
                        # Force invalid transformed coordinates outside the sampled tile.
                        src_row = cp.where(finite_gpu, src_row, cp.float32(-1e9))
                        src_col = cp.where(finite_gpu, src_col, cp.float32(-1e9))

                    out_bands = []
                    for bidx in range(src.count):
                        out_gpu = _gpu_sample_band(
                            cp, map_coordinates, src_gpu[bidx], src_row, src_col,
                            order, nodata, src.dtypes[bidx]
                        )
                        out_bands.append(cp.asnumpy(out_gpu))
                    dst.write(np.stack(out_bands, axis=0), window=Window(x0, y0, w, h))

                    del src_gpu, src_row, src_col, out_bands
                    try:
                        cp.get_default_memory_pool().free_all_blocks()
                    except Exception:
                        pass

    return {"processor_used": "GPU", "gpu_name": gpu_name, "tile_size": tile}


def align_to_reference(reference: str, moving: str, output: str, resampling: str = "bilinear", processor: str = "cpu", cancel=None) -> Dict:
    """
    Align image 2 to the exact CRS / transform / pixel grid / extent of image 1.

    processor:
      - "cpu": GDAL/Rasterio multithreaded streaming.
      - "gpu": CuPy CUDA tiled interpolation on NVIDIA GPU.
    """
    resampling_map = {
        "nearest": Resampling.nearest,
        "bilinear": Resampling.bilinear,
        "cubic": Resampling.cubic,
    }
    resampling_name = str(resampling).lower()
    method = resampling_map.get(resampling_name, Resampling.bilinear)
    processor = str(processor or "cpu").strip().lower()
    if processor not in ("cpu", "gpu"):
        processor = "cpu"

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    from preparation import check_cancel
    check_cancel(cancel)
    with rasterio.open(reference) as ref, rasterio.open(moving) as src:
        if ref.crs is None or src.crs is None:
            raise RuntimeError("توحيد الشبكة يحتاج CRS صالحًا في الصورتين.")

        if os.path.realpath(output).casefold() in {os.path.realpath(reference).casefold(),
                                                   os.path.realpath(moving).casefold()}:
            raise ValueError('Output must differ from both input rasters.')
        fd, temporary = tempfile.mkstemp(suffix='.tif', dir=os.path.dirname(os.path.abspath(output)))
        os.close(fd)
        try:
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                if processor == "gpu":
                    proc_info = _align_gpu(ref, src, temporary, method, resampling_name, cancel)
                else:
                    proc_info = _align_cpu(ref, src, temporary, method, cancel)
                from rasterio.vrt import WarpedVRT
                from rasterio.enums import ColorInterp
                with WarpedVRT(src, crs=ref.crs, transform=ref.transform,
                               width=ref.width, height=ref.height,
                               add_alpha=ColorInterp.alpha not in src.colorinterp) as vrt:
                    with rasterio.open(temporary, 'r+') as dst:
                        for _, window in dst.block_windows(1):
                            check_cancel(cancel)
                            dst.write_mask(vrt.dataset_mask(window=window), window=window)
            with rasterio.open(temporary) as check:
                if (check.crs != ref.crs or check.transform != ref.transform
                        or check.shape != ref.shape):
                    raise RuntimeError('Output grid verification failed.')
                for _, window in check.block_windows(1):
                    check_cancel(cancel)
                    check.read(window=window)
            check_cancel(cancel)
            os.replace(temporary, output)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

        result = {
            "output": output,
            "width": int(ref.width),
            "height": int(ref.height),
            "crs": _crs_text(ref.crs),
            "pixel_size_x": float(ref.res[0]),
            "pixel_size_y": float(ref.res[1]),
            "extent": list(ref.bounds),
            "full_resolution": True,
            "python_only": True,
            "processor_requested": processor.upper(),
        }
        result.update(proc_info)
        return result


def radiometric_match(reference: str, moving: str, output: str,
                      quantiles: int = 257, sample_max_side: int = 2048,
                      tile_size: int = 1024, cancel=None) -> Dict:
    """Publish a validated result atomically; cancellation never replaces input."""
    from preparation import check_cancel
    from preparation import inspect_raster, band_compatibility
    compatibility=band_compatibility(inspect_raster(reference),inspect_raster(moving))
    if compatibility['mode']=='STRUCTURAL COMPARISON':
        raise ValueError('RGB ↔ NIR / Structural: لا يسمح بالتوحيد الإشعاعي العادي.')
    if os.path.realpath(output).casefold() in {os.path.realpath(reference).casefold(),os.path.realpath(moving).casefold()}:
        raise ValueError('لا يمكن حفظ المطابقة فوق إحدى الصورتين الأصليتين.')
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    fd,tmp=tempfile.mkstemp(suffix='.tif',dir=os.path.dirname(os.path.abspath(output)));os.close(fd)
    try:
        check_cancel(cancel)
        result=_radiometric_match_impl(reference,moving,tmp,quantiles,sample_max_side,tile_size,cancel)
        with rasterio.open(tmp) as ds:
            for _,win in ds.block_windows(1):
                check_cancel(cancel);ds.read(window=win)
        check_cancel(cancel);os.replace(tmp,output);result['output']=output
        return result
    finally:
        if os.path.exists(tmp):os.remove(tmp)


def _radiometric_valid_mask(data: np.ndarray, nodata) -> np.ndarray:
    """Return valid pixels for radiometric statistics/application.

    When nodata is not declared, an all-zero multi-band pixel is treated as the
    black border produced by geometric warping and is excluded from matching.
    """
    if data.ndim != 3:
        raise ValueError("Expected band-first raster data.")
    valid = np.all(np.isfinite(data), axis=0)
    if nodata is not None:
        valid &= np.all(data != nodata, axis=0)
    else:
        valid &= np.any(data != 0, axis=0)
    return valid


def _radiometric_sample(src, max_side: int = 2048) -> np.ndarray:
    """Read a bounded-size nearest-neighbour sample for robust statistics."""
    scale = min(1.0, float(max_side) / float(max(src.width, src.height)))
    out_w = max(1, int(round(src.width * scale)))
    out_h = max(1, int(round(src.height * scale)))
    return src.read(
        out_shape=(src.count, out_h, out_w),
        resampling=Resampling.nearest,
    )


def _radiometric_match_impl(reference: str, moving: str, output: str,
                       quantiles: int = 257, sample_max_side: int = 2048,
                       tile_size: int = 1024, cancel=None) -> Dict:
    """Match image-2 radiometry to image-1 without changing its GIS grid.

    The mapping is calculated independently per band using a robust percentile
    (quantile/CDF) curve.  The full raster is then processed in tiles, so the
    whole image is never loaded into RAM.  Black warp borders / declared NoData
    pixels are excluded from the statistics and preserved as NoData/zero.

    This function intentionally requires identical CRS, transform, width and
    height.  It is designed to run *after* ``align_to_reference``.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    quantiles = max(33, int(quantiles))
    sample_max_side = max(256, int(sample_max_side))
    tile_size = max(256, int(tile_size))

    with rasterio.open(reference) as ref, rasterio.open(moving) as src:
        if ref.crs is None or src.crs is None:
            raise RuntimeError("المطابقة الإشعاعية تحتاج CRS صالحًا في الصورتين.")
        if ref.crs != src.crs:
            raise RuntimeError("CRS غير متطابق. نفّذ توحيد الشبكة أولًا.")
        if ref.width != src.width or ref.height != src.height:
            raise RuntimeError("حجم الشبكة غير متطابق. نفّذ توحيد الصورة 2 على شبكة الصورة 1 أولًا.")
        if not ref.transform.almost_equals(src.transform, precision=1e-12):
            raise RuntimeError("Pixel / Extent غير متطابق. نفّذ توحيد الشبكة أولًا.")
        if ref.count != src.count:
            raise RuntimeError(
                f"عدد الحزم غير متطابق: الصورة 1 = {ref.count}، الصورة 2 = {src.count}."
            )

        ref_sample = _radiometric_sample(ref, sample_max_side)
        src_sample = _radiometric_sample(src, sample_max_side)
        ref_valid = _radiometric_valid_mask(ref_sample, ref.nodata)
        src_valid = _radiometric_valid_mask(src_sample, src.nodata)
        if not np.any(ref_valid):
            raise RuntimeError("لا توجد بكسلات صالحة كافية في الصورة 1 للمطابقة الإشعاعية.")
        if not np.any(src_valid):
            raise RuntimeError("لا توجد بكسلات صالحة كافية في الصورة 2 للمطابقة الإشعاعية.")

        q = np.linspace(0.0, 1.0, quantiles, dtype=np.float64)
        mappings = []
        band_stats = []
        for band in range(ref.count):
            from preparation import check_cancel
            check_cancel(cancel)
            rv = np.asarray(ref_sample[band][ref_valid], dtype=np.float64)
            sv = np.asarray(src_sample[band][src_valid], dtype=np.float64)
            if rv.size < 32 or sv.size < 32:
                raise RuntimeError(f"الحزمة {band + 1} لا تحتوي عينات صالحة كافية.")

            rq = np.quantile(rv, q)
            sq = np.quantile(sv, q)
            # np.interp requires a strictly increasing x-axis.  Quantized images
            # often create repeated source quantiles, so collapse them safely.
            uq, first_idx = np.unique(sq, return_index=True)
            ur = rq[first_idx]
            if uq.size < 2:
                raise RuntimeError(f"الحزمة {band + 1} لا تحتوي مدى إشعاعي كافيًا للمطابقة.")
            mappings.append((uq.astype(np.float64), ur.astype(np.float64)))
            band_stats.append({
                "band": band + 1,
                "reference_p02": float(np.quantile(rv, 0.02)),
                "reference_p50": float(np.quantile(rv, 0.50)),
                "reference_p98": float(np.quantile(rv, 0.98)),
                "moving_p02": float(np.quantile(sv, 0.02)),
                "moving_p50": float(np.quantile(sv, 0.50)),
                "moving_p98": float(np.quantile(sv, 0.98)),
            })

        out_dtype = np.dtype(ref.dtypes[0])
        if any(np.dtype(dt) != out_dtype for dt in ref.dtypes):
            raise RuntimeError("أنواع بيانات حزم الصورة 1 مختلفة؛ لا يمكن إنشاء GeoTIFF موحد بأمان.")

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            dtype=out_dtype.name,
            width=ref.width,
            height=ref.height,
            count=ref.count,
            crs=ref.crs,
            transform=ref.transform,
            BIGTIFF="IF_SAFER",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            compress="LZW",
        )
        # Use reference NoData only when it can be represented in the output dtype.
        out_nodata = ref.nodata
        if out_nodata is not None:
            try:
                if np.issubdtype(out_dtype, np.integer):
                    info = np.iinfo(out_dtype)
                    if not (info.min <= out_nodata <= info.max):
                        out_nodata = None
            except Exception:
                out_nodata = None
        profile.update(nodata=out_nodata)

        if np.issubdtype(out_dtype, np.integer):
            dtype_min, dtype_max = np.iinfo(out_dtype).min, np.iinfo(out_dtype).max
        else:
            dtype_min, dtype_max = -np.inf, np.inf
        fill_value = 0 if out_nodata is None else out_nodata

        with rasterio.open(output, "w", **lossless_profile(profile)) as dst:
            _copy_band_metadata(ref, dst)
            for y0 in range(0, src.height, tile_size):
                check_cancel(cancel)
                h = min(tile_size, src.height - y0)
                for x0 in range(0, src.width, tile_size):
                    w = min(tile_size, src.width - x0)
                    window = Window(x0, y0, w, h)
                    check_cancel(cancel)
                    data = src.read(window=window)
                    valid = _radiometric_valid_mask(data, src.nodata)
                    out = np.full((src.count, h, w), fill_value, dtype=out_dtype)
                    if np.any(valid):
                        for band in range(src.count):
                            xp, fp = mappings[band]
                            values = np.asarray(data[band][valid], dtype=np.float64)
                            matched = np.interp(values, xp, fp, left=fp[0], right=fp[-1])
                            matched = np.clip(matched, dtype_min, dtype_max)
                            if np.issubdtype(out_dtype, np.integer):
                                matched = np.rint(matched)
                            out[band][valid] = matched.astype(out_dtype, copy=False)
                    dst.write(out, window=window)
            dst.update_tags(
                RADIOMETRIC_MATCH="quantile_cdf_per_band",
                RADIOMETRIC_REFERENCE=os.path.basename(reference),
                RADIOMETRIC_SOURCE=os.path.basename(moving),
                RADIOMETRIC_QUANTILES=str(quantiles),
            )

        return {
            "output": output,
            "method": "quantile_cdf_per_band",
            "width": int(ref.width),
            "height": int(ref.height),
            "bands": int(ref.count),
            "crs": _crs_text(ref.crs),
            "pixel_size_x": float(ref.res[0]),
            "pixel_size_y": float(ref.res[1]),
            "output_dtype": out_dtype.name,
            "sample_max_side": sample_max_side,
            "quantiles": quantiles,
            "tile_size": tile_size,
            "preserved_grid": True,
            "band_stats": band_stats,
        }
