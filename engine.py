from __future__ import annotations
import os, json, math
from pathlib import Path
import cv2
import numpy as np
from raster_profiles import lossless_profile
# Keep OpenCV's own worker pool conservative inside the desktop app.
# This is not the unsafe KMP_DUPLICATE_LIB_OK workaround; it simply avoids
# oversubscription while GUI/background threads are active.
try:
    cv2.setNumThreads(1)
except Exception:
    pass
from skimage.metrics import structural_similarity

try:
    import rasterio
    from rasterio.features import shapes
except Exception:
    rasterio = None
    shapes = None

class ImageData:
    def __init__(self, path, array, profile=None, transform=None, crs=None):
        self.path = path
        self.array = array
        self.profile = profile
        self.transform = transform
        self.crs = crs

    @property
    def width(self): return int(self.array.shape[1])
    @property
    def height(self): return int(self.array.shape[0])


def _to_uint8(arr):
    """Convert an image array to display/analysis RGB uint8 without huge temporary copies.

    ChangeVision commonly opens multi-gigapixel GeoTIFFs.  The old implementation
    converted the whole raster to float32 before stretching, which could require
    many extra gigabytes and make Rasterio fall back silently to OpenCV.  This
    implementation keeps native uint8 data zero-copy where possible and processes
    higher bit-depth rasters one channel at a time in row chunks.
    """
    arr = np.asarray(arr)
    if arr.ndim == 2:
        arr = arr[:, :, None]
    if arr.ndim != 3:
        raise ValueError(f'أبعاد الصورة غير مدعومة: {arr.shape}')

    # ChangeVision analyses RGB only.  Ignore an alpha/NIR fourth band for the
    # normal RGB view, while the original GeoTIFF remains untouched on disk.
    arr = arr[:, :, :3]

    if arr.dtype == np.uint8:
        out = np.ascontiguousarray(arr)
        if out.shape[2] == 1:
            out = np.repeat(out, 3, axis=2)
        elif out.shape[2] == 2:
            out = np.dstack((out, out[:, :, 1]))
        return out[:, :, :3]

    h, w, c = arr.shape
    out = np.zeros((h, w, max(3, c)), dtype=np.uint8)
    # Estimate robust stretch from a bounded sample, not from every pixel.
    stride = max(1, int(max(h, w) / 2048))
    for b in range(c):
        band = arr[:, :, b]
        sample = np.asarray(band[::stride, ::stride])
        finite = np.isfinite(sample)
        if not np.any(finite):
            continue
        vals = sample[finite].astype(np.float64, copy=False)
        lo, hi = np.percentile(vals, (2, 98))
        if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
            continue
        gain = 255.0 / float(hi - lo)
        # Chunked conversion keeps peak RAM bounded for huge rasters.
        rows_per_chunk = 1024
        for y0 in range(0, h, rows_per_chunk):
            y1 = min(h, y0 + rows_per_chunk)
            chunk = np.asarray(band[y0:y1], dtype=np.float32)
            chunk = (chunk - float(lo)) * float(gain)
            np.clip(chunk, 0.0, 255.0, out=chunk)
            out[y0:y1, :, b] = chunk.astype(np.uint8)
    if c == 1:
        out[:, :, 1] = out[:, :, 0]
        out[:, :, 2] = out[:, :, 0]
    elif c == 2:
        out[:, :, 2] = out[:, :, 1]
    return np.ascontiguousarray(out[:, :, :3])

def cv_imread_unicode(path: str, flags=cv2.IMREAD_UNCHANGED):
    """Read images from Unicode/Arabic Windows paths reliably."""
    try:
        data = np.fromfile(os.fspath(path), dtype=np.uint8)
    except Exception as exc:
        raise ValueError(f'تعذر الوصول إلى ملف الصورة: {path}\n{exc}') from exc
    if data.size == 0:
        raise ValueError(f'ملف الصورة فارغ أو غير قابل للقراءة: {path}')
    img = cv2.imdecode(data, flags)
    if img is None:
        raise ValueError(f'تعذر فك ترميز الصورة: {path}')
    return img


def cv_imwrite_unicode(path: str, image, params=None):
    """Write images to Unicode/Arabic Windows paths reliably."""
    ext = os.path.splitext(os.fspath(path))[1].lower() or '.png'
    if ext not in {'.png','.jpg','.jpeg','.bmp','.webp','.tif','.tiff'}:
        ext = '.png'
    ok, encoded = cv2.imencode(ext, image, params or [])
    if not ok:
        raise ValueError(f'تعذر ترميز الصورة للحفظ: {path}')
    try:
        encoded.tofile(os.fspath(path))
    except Exception as exc:
        raise ValueError(f'تعذر حفظ الصورة: {path}\n{exc}') from exc
    return True


def _recover_sidecar_georef(path):
    """Recover CRS/transform from common ArcGIS sidecars when TIFF tags are incomplete."""
    recovered_crs = None
    recovered_transform = None
    if rasterio is None:
        return recovered_crs, recovered_transform

    # ArcGIS often stores WKT in <image>.tif.aux.xml.
    aux_candidates = [path + '.aux.xml', os.path.splitext(path)[0] + '.aux.xml']
    for aux in aux_candidates:
        if not os.path.isfile(aux):
            continue
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(aux).getroot()
            for elem in root.iter():
                tag = str(elem.tag).split('}')[-1].upper()
                text = (elem.text or '').strip()
                if tag in {'SRS', 'SPATIALREFERENCE', 'WKT'} and text:
                    try:
                        recovered_crs = rasterio.crs.CRS.from_wkt(text)
                        break
                    except Exception:
                        try:
                            recovered_crs = rasterio.crs.CRS.from_string(text)
                            break
                        except Exception:
                            pass
            if recovered_crs is not None:
                break
        except Exception:
            pass

    # World file order: A, D, B, E, C, F. C/F are pixel-center coordinates.
    stem, _ = os.path.splitext(path)
    world_candidates = [stem + '.tfw', stem + '.tifw', path + 'w']
    for wf in world_candidates:
        if not os.path.isfile(wf):
            continue
        try:
            vals = [float(x.strip()) for x in Path(wf).read_text(encoding='utf-8-sig').splitlines()[:6]]
            if len(vals) == 6:
                A, D, B, E, C, F = vals
                recovered_transform = rasterio.Affine(
                    A, B, C - (A + B) / 2.0,
                    D, E, F - (D + E) / 2.0,
                )
                break
        except Exception:
            pass
    return recovered_crs, recovered_transform


def read_image(path: str) -> ImageData:
    """Open an image while preserving GeoTIFF metadata and avoiding needless RAM spikes."""
    path = os.fspath(path)
    ext = os.path.splitext(path)[1].lower()
    rio_error = None
    profile = transform = crs = None

    if ext in {'.tif', '.tiff'} and rasterio is not None:
        try:
            with rasterio.open(path) as src:
                profile = src.profile.copy()
                transform = src.transform
                crs = src.crs
                side_crs, side_transform = _recover_sidecar_georef(path)
                if crs is None and side_crs is not None:
                    crs = side_crs
                    profile['crs'] = crs
                # Identity-like transforms usually mean georeferencing was left in a world file.
                try:
                    is_identity = transform is None or (abs(transform.a - 1.0) < 1e-12 and abs(transform.e - 1.0) < 1e-12 and abs(transform.b) < 1e-12 and abs(transform.d) < 1e-12)
                except Exception:
                    is_identity = transform is None
                if is_identity and side_transform is not None:
                    transform = side_transform
                    profile['transform'] = transform
                if src.count < 1:
                    raise ValueError('GeoTIFF لا يحتوي على أي Band صالح.')
                # Read only the channels used by the normal RGB analysis/view.
                indexes = [1] if src.count == 1 else list(range(1, min(3, src.count) + 1))
                selected_dtypes = [np.dtype(src.dtypes[i - 1]) for i in indexes]
                if all(dt == np.dtype(np.uint8) for dt in selected_dtypes):
                    # Read one band at a time into the final HWC buffer.  This avoids
                    # keeping both a huge band-first array and a huge transposed copy.
                    rgb = np.empty((src.height, src.width, 3), dtype=np.uint8)
                    for out_band, src_band in enumerate(indexes):
                        tmp = src.read(src_band)
                        rgb[:, :, out_band] = tmp
                        del tmp
                    if len(indexes) == 1:
                        rgb[:, :, 1] = rgb[:, :, 0]
                        rgb[:, :, 2] = rgb[:, :, 0]
                    elif len(indexes) == 2:
                        rgb[:, :, 2] = rgb[:, :, 1]
                    rgb[src.dataset_mask() == 0] = 0
                    return ImageData(path, rgb, profile, transform, crs)
                arr = src.read(indexes=indexes)
                raster_valid = src.dataset_mask() > 0
            arr = np.transpose(arr, (1, 2, 0))
            rgb = _to_uint8(arr)
            rgb[~raster_valid] = 0
            return ImageData(path, rgb, profile, transform, crs)
        except Exception as exc:
            # Keep the real Rasterio error.  We may still be able to decode pixels
            # through OpenCV, but must not silently discard valid georeferencing.
            rio_error = exc

    try:
        bgr = cv_imread_unicode(path, cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        if ext in {'.tif', '.tiff'}:
            side_crs, side_transform = _recover_sidecar_georef(path)
            crs = crs or side_crs
            transform = transform or side_transform
            if profile is None and (crs is not None or transform is not None):
                profile = {
                    'driver': 'GTiff', 'height': rgb.shape[0], 'width': rgb.shape[1],
                    'count': 3, 'dtype': str(rgb.dtype), 'crs': crs, 'transform': transform,
                }
        return ImageData(path, rgb, profile, transform, crs)
    except Exception as cv_exc:
        if rio_error is not None:
            raise RuntimeError(
                'تعذر فتح الصورة بواسطة Rasterio وOpenCV.\n'
                f'Rasterio: {rio_error}\nOpenCV: {cv_exc}'
            ) from cv_exc
        raise

def resize_like(reference, moving):
    h, w = reference.shape[:2]
    if moving.shape[:2] == (h, w):
        return moving.copy()
    return cv2.resize(moving, (w, h), interpolation=cv2.INTER_LINEAR)


def _registration_proxy(reference, moving, max_dim=2200):
    """Create a same-scale proxy for feature detection on very large images."""
    moving = resize_like(reference, moving)
    h, w = reference.shape[:2]
    scale = min(1.0, float(max_dim) / max(h, w))
    if scale < 0.999:
        size = (max(64, int(round(w * scale))), max(64, int(round(h * scale))))
        ref_p = cv2.resize(reference, size, interpolation=cv2.INTER_AREA)
        mov_p = cv2.resize(moving, size, interpolation=cv2.INTER_AREA)
    else:
        ref_p, mov_p = reference, moving
    return moving, ref_p, mov_p, scale


def _reprojection_error(H, src, dst, inlier_mask=None):
    pred = cv2.perspectiveTransform(src.astype(np.float32), H)
    err = np.linalg.norm(pred.reshape(-1, 2) - dst.reshape(-1, 2), axis=1)
    if inlier_mask is not None:
        keep = np.asarray(inlier_mask).reshape(-1).astype(bool)
        if np.any(keep):
            err = err[keep]
    return float(np.median(err)) if err.size else float('inf')


def _inlier_coverage(dst, inlier_mask, width, height):
    if inlier_mask is None:
        return 0.0
    keep = np.asarray(inlier_mask).reshape(-1).astype(bool)
    pts = dst.reshape(-1, 2)[keep]
    if len(pts) < 3:
        return 0.0
    hull = cv2.convexHull(pts.astype(np.float32))
    return float(cv2.contourArea(hull) / max(float(width * height), 1.0))


def _quality_label(inlier_ratio, reproj_px, coverage, overlap):
    if inlier_ratio >= .50 and reproj_px <= 1.5 and coverage >= .25 and overlap >= .70:
        return 'ممتاز', 'EXCELLENT'
    if inlier_ratio >= .35 and reproj_px <= 2.5 and coverage >= .15 and overlap >= .60:
        return 'جيد', 'GOOD'
    if inlier_ratio >= .20 and reproj_px <= 3.5 and coverage >= .08 and overlap >= .50:
        return 'مقبول', 'ACCEPTABLE'
    return 'ضعيف', 'WEAK'


def _registration_gray_and_mask(rgb):
    """Return a contrast-stabilized grayscale image plus a non-black data mask."""
    img = np.asarray(rgb)
    if img.ndim == 2:
        gray = img.astype(np.uint8, copy=False)
        valid = gray > 2
    else:
        base = img[:, :, :min(3, img.shape[2])]
        valid = np.any(base > 2, axis=2)
        gray = cv2.cvtColor(base.astype(np.uint8, copy=False), cv2.COLOR_RGB2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray[~valid] = 0
    return np.ascontiguousarray(gray), (valid.astype(np.uint8) * 255)


def _grid_balanced_matches(matches, keypoints, width, height, grid=8, per_cell=90):
    """Keep the best descriptor matches from each spatial cell of the reference."""
    if not matches:
        return []
    cells = {}
    gw = max(float(width) / grid, 1.0)
    gh = max(float(height) / grid, 1.0)
    for m in matches:
        x, y = keypoints[m.queryIdx].pt
        cx = min(grid - 1, max(0, int(x / gw)))
        cy = min(grid - 1, max(0, int(y / gh)))
        cells.setdefault((cx, cy), []).append(m)
    out = []
    for vals in cells.values():
        vals.sort(key=lambda m: m.distance)
        out.extend(vals[:per_cell])
    out.sort(key=lambda m: m.distance)
    return out


def _ratio_matches(d_ref, d_mov, ratio, neighbors=None):
    """Lowe-ratio matches, preferring bidirectionally consistent pairs."""
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    fwd = neighbors[0] if neighbors is not None else matcher.knnMatch(d_ref, d_mov, k=2)
    good_fwd = [m for pair in fwd if len(pair) >= 2 for m, n in [pair[:2]] if m.distance < ratio * n.distance]
    if len(good_fwd) < 16:
        return good_fwd
    try:
        rev = neighbors[1] if neighbors is not None else matcher.knnMatch(d_mov, d_ref, k=2)
        back = {}
        for pair in rev:
            if len(pair) < 2:
                continue
            m, n = pair[:2]
            if m.distance < ratio * n.distance:
                back[m.queryIdx] = m.trainIdx
        mutual = [m for m in good_fwd if back.get(m.trainIdx) == m.queryIdx]
        # Mutual matching is cleaner, but do not sacrifice too much coverage.
        return mutual if len(mutual) >= max(24, int(0.45 * len(good_fwd))) else good_fwd
    except Exception:
        return good_fwd


def _candidate_from_transform(H, mask, src, dst, nm, ratio, scale, wp, hp, model_name):
    if H is None or mask is None:
        return None
    H = np.asarray(H, dtype=np.float64)
    nin = int(np.asarray(mask).reshape(-1).sum())
    if nin < 8:
        return None
    ir = nin / max(nm, 1)
    err_proxy = _reprojection_error(H, src, dst, mask)
    err_full = err_proxy / max(scale, 1e-9)
    cov = _inlier_coverage(dst, mask, wp, hp)
    score = (ir ** 1.40) * np.log1p(nin) * (0.30 + min(cov, .70)) / (1.0 + err_full)
    return dict(ratio=float(ratio), matches=int(nm), inliers=int(nin), inlier_ratio=float(ir),
                reprojection_error=float(err_full), coverage=float(cov), score=float(score),
                H_proxy=H, src=src, dst=dst, mask=mask, model=model_name)


def _estimate_initial_transform(ref_p, mov_p, scale, preferred_ratio, reproj, report_progress):
    """SIFT + ratio sweep + MAGSAC/Affine model selection."""
    f1, m1 = _registration_gray_and_mask(ref_p)
    f2, m2 = _registration_gray_and_mask(mov_p)
    report_progress(16, 'استخراج نقاط SIFT موزعة على كامل منطقة البيانات...')
    sift = cv2.SIFT_create(nfeatures=32000, contrastThreshold=0.012, edgeThreshold=14,
                           sigma=1.6)
    k1, d1 = sift.detectAndCompute(f1, m1)
    k2, d2 = sift.detectAndCompute(f2, m2)
    if d1 is None or d2 is None or len(k1) < 12 or len(k2) < 12:
        raise RuntimeError('تعذر استخراج نقاط مميزة كافية للمواءمة المتقدمة.')

    report_progress(27, 'مطابقة السمات واختيار أفضل نموذج هندسي...')
    ratios = sorted(set(round(float(x), 2) for x in
                        [0.58,0.62,0.65,0.68,0.70,0.72,0.75,0.78,0.80,0.82,float(preferred_ratio)]
                        if .50 <= float(x) <= .95))
    method = getattr(cv2, 'USAC_MAGSAC', cv2.RANSAC)
    hp, wp = f1.shape[:2]
    best = None
    candidates = []
    # A tighter proxy threshold is important when the final goal is sub-2-pixel registration.
    ransac_thr = max(0.65, min(2.5, float(reproj) * max(scale, 0.20) * 0.90))
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    neighbors = (matcher.knnMatch(d1, d2, k=2), matcher.knnMatch(d2, d1, k=2))

    for idx, ratio in enumerate(ratios):
        good = _ratio_matches(d1, d2, ratio, neighbors)
        good = _grid_balanced_matches(good, k1, wp, hp, grid=8, per_cell=95)
        if len(good) < 12:
            continue
        # src: MOVING proxy, dst: REFERENCE proxy. H maps moving -> reference.
        src = np.float32([k2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([k1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)

        try:
            Hh, mh = cv2.findHomography(src, dst, method, ransac_thr, maxIters=15000, confidence=0.9995)
        except TypeError:
            Hh, mh = cv2.findHomography(src, dst, method, ransac_thr)
        cand_h = _candidate_from_transform(Hh, mh, src, dst, len(good), ratio, scale, wp, hp, 'HOMOGRAPHY')
        if cand_h is not None:
            candidates.append(cand_h)

        # A similarity/affine model is often more stable for orthorectified aerial imagery.
        try:
            A, ma = cv2.estimateAffinePartial2D(
                src.reshape(-1, 2), dst.reshape(-1, 2), method=cv2.RANSAC,
                ransacReprojThreshold=ransac_thr, maxIters=15000,
                confidence=0.9995, refineIters=50)
            if A is not None:
                Ha = np.eye(3, dtype=np.float64); Ha[:2, :] = A
                cand_a = _candidate_from_transform(Ha, ma, src, dst, len(good), ratio, scale, wp, hp, 'AFFINE_PARTIAL')
                if cand_a is not None:
                    # Slight stability bonus for the lower-DOF model when geometry is comparable.
                    cand_a['score'] *= 1.04
                    candidates.append(cand_a)
        except Exception:
            pass

        if candidates:
            best = max(candidates, key=lambda c: c['score'])
        report_progress(28 + int(22 * (idx + 1) / max(len(ratios),1)), f'اختبار SIFT ratio={ratio:.2f}')

    if best is None:
        raise RuntimeError('لم تنجح أي قيمة SIFT ratio في إنشاء تحويل هندسي موثوق.')
    if best['inliers'] < 30 or best['inlier_ratio'] < 0.20 or best['coverage'] < 0.04:
        raise RuntimeError(
            'تم رفض المواءمة الأولية: التطابقات غير موزعة أو غير موثوقة بما يكفي. '
            f"Inliers={best['inliers']}، ratio={best['inlier_ratio']*100:.1f}%، coverage={best['coverage']*100:.1f}%."
        )
    return best, candidates


def _residual_sift_refine(ref_p, mov_p, H_proxy, report_progress, passes=2):
    """Run SIFT again after the coarse warp and estimate only a small residual transform."""
    hp, wp = ref_p.shape[:2]
    current = np.asarray(H_proxy, dtype=np.float64)
    used = 0
    last_error = None
    last_inliers = 0
    last_coverage = 0.0
    for pidx in range(max(1, int(passes))):
        warped = cv2.warpPerspective(mov_p, current, (wp, hp), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        fr, mr = _registration_gray_and_mask(ref_p)
        fw, mw = _registration_gray_and_mask(warped)
        mask = cv2.bitwise_and(mr, mw)
        sift = cv2.SIFT_create(nfeatures=22000, contrastThreshold=0.012, edgeThreshold=14)
        kr, dr = sift.detectAndCompute(fr, mask)
        kw, dw = sift.detectAndCompute(fw, mask)
        if dr is None or dw is None or len(kr) < 12 or len(kw) < 12:
            break
        good = _ratio_matches(dr, dw, 0.76)
        good = _grid_balanced_matches(good, kr, wp, hp, grid=8, per_cell=80)
        if len(good) < 20:
            break
        src = np.float32([kw[m.trainIdx].pt for m in good]).reshape(-1, 2)
        dst = np.float32([kr[m.queryIdx].pt for m in good]).reshape(-1, 2)
        try:
            A, inl = cv2.estimateAffinePartial2D(
                src, dst, method=cv2.RANSAC, ransacReprojThreshold=1.25,
                maxIters=12000, confidence=0.9995, refineIters=80)
        except Exception:
            A, inl = None, None
        if A is None or inl is None or int(inl.sum()) < 20:
            break
        C = np.eye(3, dtype=np.float64); C[:2, :] = A
        # Residual corrections should be small. Reject a wild secondary model.
        rot = math.degrees(math.atan2(float(C[1,0]), float(C[0,0])))
        scl = math.hypot(float(C[0,0]), float(C[1,0]))
        trans = math.hypot(float(C[0,2]), float(C[1,2]))
        if abs(rot) > 1.5 or not (0.985 <= scl <= 1.015) or trans > 0.035 * max(wp, hp):
            break
        err_before = _reprojection_error(np.eye(3), src.reshape(-1,1,2), dst.reshape(-1,1,2), inl)
        err_after = _reprojection_error(C, src.reshape(-1,1,2), dst.reshape(-1,1,2), inl)
        if not np.isfinite(err_after) or err_after > err_before * 0.92:
            break
        current = C @ current
        used += 1
        last_error = float(err_after)
        last_inliers = int(inl.sum())
        last_coverage = _inlier_coverage(dst.reshape(-1,1,2), inl, wp, hp)
        report_progress(52 + pidx * 5, f'تحسين SIFT المتبقي {pidx+1}: error={err_after:.3f}px على نسخة التحليل')
    return current, used, last_error, last_inliers, last_coverage


def _ecc_feature(gray):
    """Gradient-rich float image for ECC; more stable across sensor/brightness differences."""
    g = gray.astype(np.float32) / 255.0
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mag = cv2.GaussianBlur(mag, (0,0), 0.8)
    lo, hi = np.percentile(mag, (2.0, 98.0))
    if hi > lo + 1e-8:
        mag = np.clip((mag - lo) / (hi - lo), 0.0, 1.0)
    return np.ascontiguousarray(mag.astype(np.float32))


def _masked_corr(a, b, mask):
    keep = mask > 0
    if np.count_nonzero(keep) < 1000:
        return -1.0
    x = a[keep].astype(np.float64); y = b[keep].astype(np.float64)
    x -= x.mean(); y -= y.mean()
    den = math.sqrt(float(np.dot(x,x) * np.dot(y,y)))
    return float(np.dot(x,y) / den) if den > 1e-12 else -1.0


def _ecc_multiscale_refine(ref_p, mov_p, H_proxy, report_progress):
    """Conservative multi-scale residual ECC in gradient space."""
    hp, wp = ref_p.shape[:2]
    current = np.asarray(H_proxy, dtype=np.float64)
    ecc_scores = []
    used = 0
    levels = [0.35, 0.60, 1.00]
    for li, lev in enumerate(levels):
        lw = max(320, int(round(wp * lev))); lh = max(240, int(round(hp * lev)))
        S = np.array([[lev,0,0],[0,lev,0],[0,0,1]], dtype=np.float64)
        Hlev = S @ current @ np.linalg.inv(S)
        r = cv2.resize(ref_p, (lw,lh), interpolation=cv2.INTER_AREA if lev < 1 else cv2.INTER_LINEAR)
        m = cv2.resize(mov_p, (lw,lh), interpolation=cv2.INTER_AREA if lev < 1 else cv2.INTER_LINEAR)
        rg, rm = _registration_gray_and_mask(r)
        mg, mm = _registration_gray_and_mask(m)
        warped = cv2.warpPerspective(mg, Hlev, (lw,lh), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        v = cv2.warpPerspective(mm, Hlev, (lw,lh), flags=cv2.INTER_NEAREST,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        mask = cv2.bitwise_and(rm, v)
        if np.count_nonzero(mask) < 0.08 * mask.size:
            continue
        tf = _ecc_feature(rg); inf = _ecc_feature(warped)
        before_corr = _masked_corr(tf, inf, mask)
        W = np.eye(2,3,dtype=np.float32)
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 90 if lev < 1 else 60, 2e-6)
        try:
            cc, W = cv2.findTransformECC(tf, inf, W, cv2.MOTION_AFFINE, criteria,
                                         inputMask=mask, gaussFiltSize=5)
        except Exception:
            continue
        W3 = np.eye(3,dtype=np.float64); W3[:2,:] = W
        try:
            Clev = np.linalg.inv(W3)
        except np.linalg.LinAlgError:
            continue
        C = np.linalg.inv(S) @ Clev @ S
        rot = math.degrees(math.atan2(float(C[1,0]), float(C[0,0])))
        scl = math.hypot(float(C[0,0]), float(C[1,0]))
        trans = math.hypot(float(C[0,2]), float(C[1,2]))
        if abs(rot) > 1.0 or not (0.990 <= scl <= 1.010) or trans > 18.0:
            continue
        testH = C @ current
        test_warp = cv2.warpPerspective(mg, S @ testH @ np.linalg.inv(S), (lw,lh),
                                        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        test_v = cv2.warpPerspective(mm, S @ testH @ np.linalg.inv(S), (lw,lh),
                                     flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        test_mask = cv2.bitwise_and(rm, test_v)
        after_corr = _masked_corr(tf, _ecc_feature(test_warp), test_mask)
        # ECC may return a numerical solution that is bad for cross-date imagery; accept only improvement.
        if after_corr >= before_corr + 0.002 or (before_corr > 0.45 and after_corr >= before_corr - 0.001):
            current = testH
            used += 1
            ecc_scores.append(float(cc))
            report_progress(64 + li * 4, f'ECC متعدد المستويات {int(lev*100)}%: corr={after_corr:.4f}')
    return current, (ecc_scores[-1] if ecc_scores else None), used


def _phase_tile_metrics(reference, aligned, valid_mask=None, grid=4, tile_size=640, max_shift=18.0):
    """Measure residual local translations with gradient phase correlation."""
    h, w = reference.shape[:2]
    if aligned.shape[:2] != (h,w):
        aligned = cv2.resize(aligned, (w,h), interpolation=cv2.INTER_LINEAR)
    if valid_mask is None:
        vm = np.ones((h,w), np.uint8) * 255
    else:
        vm = valid_mask[:, :, 0] if getattr(valid_mask, 'ndim', 2) == 3 else valid_mask
        if vm.shape[:2] != (h,w):
            vm = cv2.resize(vm, (w,h), interpolation=cv2.INTER_NEAREST)
    ts = int(min(tile_size, max(192, min(h,w)//3)))
    half = ts // 2
    if w <= ts+8 or h <= ts+8:
        centers = [(w//2, h//2)]
    else:
        xs = np.linspace(half+2, w-half-3, grid).astype(int)
        ys = np.linspace(half+2, h-half-3, grid).astype(int)
        centers = [(int(x),int(y)) for y in ys for x in xs]
    shifts = []
    window = cv2.createHanningWindow((ts,ts), cv2.CV_32F)
    for cx,cy in centers:
        x0=cx-half; y0=cy-half; x1=x0+ts; y1=y0+ts
        if x0<0 or y0<0 or x1>w or y1>h:
            continue
        m = vm[y0:y1,x0:x1]
        if np.count_nonzero(m) < 0.70 * m.size:
            continue
        r = reference[y0:y1,x0:x1]
        a = aligned[y0:y1,x0:x1]
        rg = cv2.cvtColor(r[:,:,:3], cv2.COLOR_RGB2GRAY) if r.ndim==3 else r
        ag = cv2.cvtColor(a[:,:,:3], cv2.COLOR_RGB2GRAY) if a.ndim==3 else a
        # High-pass luminance is robust against global brightness and color differences.
        rf = rg.astype(np.float32) - cv2.GaussianBlur(rg.astype(np.float32),(0,0),3.0)
        af = ag.astype(np.float32) - cv2.GaussianBlur(ag.astype(np.float32),(0,0),3.0)
        if float(np.std(rf[m>0])) < 2.0 or float(np.std(af[m>0])) < 2.0:
            continue
        keep=(m>0).astype(np.float32)
        rf *= keep; af *= keep
        try:
            (dx,dy), response = cv2.phaseCorrelate(rf, af, window)
        except Exception:
            continue
        mag=math.hypot(float(dx),float(dy))
        if np.isfinite(dx) and np.isfinite(dy) and response >= 0.035 and mag <= max_shift:
            shifts.append((float(dx),float(dy),float(response),float(cx),float(cy)))
    if not shifts:
        return {'count':0,'median_dx':0.0,'median_dy':0.0,'median_mag':float('inf'),'p90_mag':float('inf'),'mad':float('inf'),'shifts':[]}
    arr=np.asarray([[s[0],s[1]] for s in shifts],dtype=np.float64)
    med=np.median(arr,axis=0)
    dev=np.linalg.norm(arr-med,axis=1)
    mad=float(np.median(dev))
    # Robustly discard isolated tiles caused by genuine scene changes.
    lim=max(1.0, 3.5*mad)
    keep=dev<=lim
    if np.count_nonzero(keep)>=3:
        arr=arr[keep]
        shifts=[s for s,k in zip(shifts,keep) if k]
        med=np.median(arr,axis=0)
        dev=np.linalg.norm(arr-med,axis=1)
        mad=float(np.median(dev))
    mags=np.linalg.norm(arr,axis=1)
    return {'count':int(len(arr)), 'median_dx':float(med[0]), 'median_dy':float(med[1]),
            'median_mag':float(np.median(mags)), 'p90_mag':float(np.percentile(mags,90)),
            'mad':float(mad), 'shifts':shifts}


def _warp_rgb_full(moving, H, size):
    w,h=size
    return cv2.warpPerspective(moving, H, (w,h), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def _local_residual_affine_refine(reference, moving_full, H_full, valid_mask, data_valid,
                                  report_progress, max_passes=2):
    """Refine spatially varying residual drift with a conservative full-resolution affine fit.

    The global SIFT/Homography/ECC stage can leave a small residual that changes gradually
    across the image (for example 1-4 px caused by sensor geometry or resampling).  This
    function measures local translations on a grid, fits ONE small affine correction from
    those control points, and accepts it only when independent phase-correlation metrics
    improve.  It therefore does not introduce a free-form/non-rigid warp and H remains a
    valid moving->reference transform.
    """
    h, w = reference.shape[:2]
    current_H = np.asarray(H_full, dtype=np.float64).copy()
    current_aligned = _warp_rgb_full(moving_full, current_H, (w, h))
    current_valid = valid_mask
    current_data_valid = data_valid
    metrics = _phase_tile_metrics(reference, current_aligned, current_data_valid,
                                  grid=5, tile_size=768, max_shift=12.0)
    used = 0
    last_inliers = 0

    for pidx in range(max(1, int(max_passes))):
        shifts = list(metrics.get('shifts') or [])
        if len(shifts) < 5:
            break

        src = np.asarray([[s[3], s[4]] for s in shifts], dtype=np.float32)
        # phaseCorrelate residual (dx,dy) is corrected by moving the already-aligned
        # tile in the opposite direction.  These are local control-point targets.
        dst = np.asarray([[s[3] - s[0], s[4] - s[1]] for s in shifts], dtype=np.float32)

        try:
            A, inl = cv2.estimateAffine2D(
                src, dst, method=cv2.RANSAC, ransacReprojThreshold=1.35,
                maxIters=10000, confidence=0.999, refineIters=60)
        except Exception:
            A, inl = None, None
        if A is None or inl is None or int(np.asarray(inl).sum()) < 4:
            try:
                A, inl = cv2.estimateAffinePartial2D(
                    src, dst, method=cv2.RANSAC, ransacReprojThreshold=1.35,
                    maxIters=10000, confidence=0.999, refineIters=60)
            except Exception:
                A, inl = None, None
        if A is None or inl is None:
            break

        nin = int(np.asarray(inl).reshape(-1).sum())
        if nin < 4:
            break
        C = np.eye(3, dtype=np.float64)
        C[:2, :] = np.asarray(A, dtype=np.float64)

        # Reject any correction that is too large to be a residual refinement.
        a, b, tx = C[0, 0], C[0, 1], C[0, 2]
        c, d, ty = C[1, 0], C[1, 1], C[1, 2]
        sx = math.hypot(a, c); sy = math.hypot(b, d)
        rot = math.degrees(math.atan2(c, a))
        shear = abs(a*b + c*d) / max(sx*sy, 1e-9)
        trans = math.hypot(tx, ty)
        if abs(rot) > 0.75 or not (0.992 <= sx <= 1.008) or not (0.992 <= sy <= 1.008) \
                or shear > 0.012 or trans > 18.0:
            break

        test_H = C @ current_H
        test_H /= test_H[2, 2]
        test_aligned = _warp_rgb_full(moving_full, test_H, (w, h))
        test_valid = cv2.warpPerspective(
            np.full(moving_full.shape[:2], 255, np.uint8), test_H, (w, h),
            flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        try:
            test_data_valid = common_data_valid_mask(
                reference, test_aligned, test_valid, black_threshold=2, rows_per_chunk=768)
        except Exception:
            test_data_valid = test_valid
        test_metrics = _phase_tile_metrics(reference, test_aligned, test_data_valid,
                                           grid=5, tile_size=768, max_shift=10.0)

        before_med = float(metrics.get('median_mag', float('inf')))
        before_p90 = float(metrics.get('p90_mag', float('inf')))
        after_med = float(test_metrics.get('median_mag', float('inf')))
        after_p90 = float(test_metrics.get('p90_mag', float('inf')))
        enough = int(test_metrics.get('count', 0)) >= 4
        # Require a real median improvement and do not trade it for a much worse tail.
        improved = enough and np.isfinite(after_med) and (
            after_med <= before_med - 0.18 or after_med <= before_med * 0.82)
        tail_ok = (not np.isfinite(before_p90)) or after_p90 <= before_p90 + 0.20
        if not (improved and tail_ok):
            break

        current_H = test_H
        current_aligned = test_aligned
        current_valid = test_valid
        current_data_valid = test_data_valid
        metrics = test_metrics
        used += 1
        last_inliers = nin
        report_progress(88 + pidx * 2,
                        f'مواءمة محلية متبقية {pidx+1}: median={after_med:.2f}px | P90={after_p90:.2f}px')

        if after_med <= 0.75 and (not np.isfinite(after_p90) or after_p90 <= 1.50):
            break

    return current_aligned, current_H, current_valid, current_data_valid, metrics, used, last_inliers


def _pre_gate_local_residual_refine(reference, moving_full, H_full, valid_mask, data_valid,
                                    report_progress, max_passes=3):
    """Mandatory pre-gate residual refinement at full resolution.

    This stage runs *before* the final quality rejection.  It measures a denser set of
    phase-correlation control points and tries only small, conservative corrections:
    a robust global translation and then a small affine/partial-affine correction.
    Every candidate is evaluated on fresh full-resolution tiles and is accepted only
    when the measured residual improves.  No free-form/non-rigid warp is introduced.
    """
    h, w = reference.shape[:2]
    current_H = np.asarray(H_full, dtype=np.float64).copy()
    current_aligned = _warp_rgb_full(moving_full, current_H, (w, h))
    current_valid = valid_mask
    current_data_valid = data_valid

    def _measure(aligned_img, dv, grid=7, tile=576, max_shift=24.0):
        return _phase_tile_metrics(reference, aligned_img, dv, grid=grid,
                                   tile_size=tile, max_shift=max_shift)

    def _score(m):
        med = float(m.get('median_mag', float('inf')))
        p90 = float(m.get('p90_mag', float('inf')))
        n = int(m.get('count', 0))
        if n < 3 or not np.isfinite(med):
            return float('inf')
        if not np.isfinite(p90):
            p90 = med * 1.8
        # Median is primary; P90 prevents accepting a correction that helps only
        # the center while making edge tiles worse.
        return med + 0.28 * p90

    def _warp_and_measure(Hcand):
        a = _warp_rgb_full(moving_full, Hcand, (w, h))
        v = cv2.warpPerspective(
            np.full(moving_full.shape[:2], 255, np.uint8), Hcand, (w, h),
            flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        try:
            dv = common_data_valid_mask(reference, a, v, black_threshold=2, rows_per_chunk=768)
        except Exception:
            dv = v
        m = _measure(a, dv)
        return a, v, dv, m

    metrics = _measure(current_aligned, current_data_valid)
    used = 0
    affine_inliers = 0
    report_progress(84, f"Local Residual قبل الفحص: median={float(metrics.get('median_mag', float('inf'))):.2f}px | P90={float(metrics.get('p90_mag', float('inf'))):.2f}px")

    for pidx in range(max(1, int(max_passes))):
        base_score = _score(metrics)
        if not np.isfinite(base_score):
            break
        if float(metrics.get('median_mag', 99.0)) <= 0.70 and float(metrics.get('p90_mag', 99.0)) < 1.50:
            break

        candidates = []
        shifts = list(metrics.get('shifts') or [])

        # 1) Robust full-resolution translation.  Evaluate both signs because
        # phase-correlation sign convention can be affected by the prior warp.
        mdx = float(metrics.get('median_dx', 0.0)); mdy = float(metrics.get('median_dy', 0.0))
        vmag = math.hypot(mdx, mdy)
        if 0.10 < vmag <= 24.0:
            for sign in (-1.0, 1.0):
                T = np.array([[1.0, 0.0, sign * mdx],
                              [0.0, 1.0, sign * mdy],
                              [0.0, 0.0, 1.0]], dtype=np.float64)
                Hc = T @ current_H
                Hc /= Hc[2, 2]
                try:
                    aa, vv, dd, mm = _warp_and_measure(Hc)
                    candidates.append((_score(mm), 'translation', 0, Hc, aa, vv, dd, mm))
                except Exception:
                    pass

        # 2) Dense local control points -> small affine / partial-affine.
        if len(shifts) >= 4:
            # Prefer more reliable control points but keep enough spatial coverage.
            resp = np.asarray([s[2] for s in shifts], dtype=np.float64)
            cut = max(0.035, float(np.percentile(resp, 30))) if resp.size else 0.035
            good = [s for s in shifts if s[2] >= cut]
            if len(good) < 4:
                good = shifts
            src_pts = np.asarray([[s[3], s[4]] for s in good], dtype=np.float32)

            for sign in (-1.0, 1.0):
                dst_pts = np.asarray([[s[3] + sign*s[0], s[4] + sign*s[1]] for s in good], dtype=np.float32)
                for partial in (True, False):
                    try:
                        if partial:
                            A, inl = cv2.estimateAffinePartial2D(
                                src_pts, dst_pts, method=cv2.RANSAC,
                                ransacReprojThreshold=1.8, maxIters=15000,
                                confidence=0.999, refineIters=80)
                        else:
                            A, inl = cv2.estimateAffine2D(
                                src_pts, dst_pts, method=cv2.RANSAC,
                                ransacReprojThreshold=1.8, maxIters=15000,
                                confidence=0.999, refineIters=80)
                    except Exception:
                        A, inl = None, None
                    if A is None or inl is None:
                        continue
                    nin = int(np.asarray(inl).reshape(-1).sum())
                    if nin < 4:
                        continue
                    C = np.eye(3, dtype=np.float64); C[:2, :] = np.asarray(A, dtype=np.float64)
                    a,b,tx = C[0,0],C[0,1],C[0,2]; c,d,ty = C[1,0],C[1,1],C[1,2]
                    sx = math.hypot(a,c); sy = math.hypot(b,d)
                    rot = math.degrees(math.atan2(c,a))
                    shear = abs(a*b + c*d) / max(sx*sy, 1e-9)
                    trans = math.hypot(tx,ty)
                    if abs(rot) > 1.35 or not (0.986 <= sx <= 1.014) or not (0.986 <= sy <= 1.014) \
                            or shear > 0.020 or trans > 26.0:
                        continue
                    Hc = C @ current_H
                    Hc /= Hc[2,2]
                    try:
                        aa, vv, dd, mm = _warp_and_measure(Hc)
                        candidates.append((_score(mm), 'affine_partial' if partial else 'affine', nin,
                                           Hc, aa, vv, dd, mm))
                    except Exception:
                        pass

        if not candidates:
            break
        candidates.sort(key=lambda x: x[0])
        best_c = candidates[0]
        best_score, kind, nin, Hc, aa, vv, dd, mm = best_c
        before_med = float(metrics.get('median_mag', float('inf')))
        before_p90 = float(metrics.get('p90_mag', float('inf')))
        after_med = float(mm.get('median_mag', float('inf')))
        after_p90 = float(mm.get('p90_mag', float('inf')))

        # Require measurable improvement.  A small median improvement is accepted
        # only when the tail also improves.
        median_gain = before_med - after_med
        tail_gain = before_p90 - after_p90 if np.isfinite(before_p90) and np.isfinite(after_p90) else 0.0
        accept = (best_score + 0.08 < base_score and median_gain >= 0.12) or \
                 (median_gain >= 0.05 and tail_gain >= 0.35)
        if not accept:
            break

        current_H = Hc; current_aligned = aa; current_valid = vv
        current_data_valid = dd; metrics = mm
        used += 1; affine_inliers = max(affine_inliers, int(nin))
        report_progress(85 + min(pidx,2)*2,
                        f'Local Residual {pidx+1} ({kind}): median={after_med:.2f}px | P90={after_p90:.2f}px')

    # One final pass through the existing conservative affine refiner can use the
    # improved control-point geometry produced above.
    if int(metrics.get('count',0)) >= 4 and float(metrics.get('median_mag',99.0)) > 0.70:
        try:
            aa, HH, vv, dd, mm, extra_used, extra_inl = _local_residual_affine_refine(
                reference, moving_full, current_H, current_valid, current_data_valid,
                report_progress, max_passes=2)
            if _score(mm) + 0.05 < _score(metrics):
                current_aligned, current_H, current_valid, current_data_valid, metrics = aa, HH, vv, dd, mm
                used += int(extra_used); affine_inliers = max(affine_inliers, int(extra_inl))
        except Exception:
            pass

    report_progress(90, f"Local Residual النهائي: median={float(metrics.get('median_mag', float('inf'))):.2f}px | P90={float(metrics.get('p90_mag', float('inf'))):.2f}px | passes={used}")
    return current_aligned, current_H, current_valid, current_data_valid, metrics, used, affine_inliers



def _poly2_design(xs, ys):
    """Quadratic 2-D design matrix for a smooth residual displacement surface."""
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    return np.column_stack((np.ones_like(xs), xs, ys, xs*ys, xs*xs, ys*ys))


def _fit_smooth_residual_surface(shifts, width, height):
    """Fit robust quadratic dx/dy surfaces to local phase-correlation shifts.

    The fit is deliberately low-order.  It can model a residual that changes slowly
    across the scene (orthorectification/sensor geometry) without allowing the free-form
    warp to chase real scene changes.
    """
    if len(shifts) < 8:
        return None
    arr = np.asarray(shifts, dtype=np.float64)
    # shift tuple: dx, dy, response, cx, cy
    dx, dy, resp, cx, cy = arr[:,0], arr[:,1], arr[:,2], arr[:,3], arr[:,4]
    xn = (cx / max(float(width-1), 1.0)) * 2.0 - 1.0
    yn = (cy / max(float(height-1), 1.0)) * 2.0 - 1.0
    X = _poly2_design(xn, yn)
    keep = np.isfinite(arr).all(axis=1) & (resp >= 0.035)
    if np.count_nonzero(keep) < 8:
        return None

    bx = by = None
    for _ in range(4):
        idx = np.flatnonzero(keep)
        if idx.size < 8:
            return None
        # Give higher-response tiles somewhat more influence, but keep a floor so
        # spatial coverage matters more than one exceptionally sharp tile.
        ww = np.clip(resp[idx], 0.04, 0.80)
        ww = np.sqrt(ww / max(float(np.median(ww)), 1e-6))
        Xw = X[idx] * ww[:,None]
        try:
            bx = np.linalg.lstsq(Xw, dx[idx] * ww, rcond=None)[0]
            by = np.linalg.lstsq(Xw, dy[idx] * ww, rcond=None)[0]
        except Exception:
            return None
        predx = X @ bx; predy = X @ by
        er = np.hypot(predx-dx, predy-dy)
        med = float(np.median(er[idx])); mad = float(np.median(np.abs(er[idx]-med)))
        lim = max(1.20, med + 3.5 * max(mad, 0.20))
        new_keep = keep & (er <= lim)
        if np.count_nonzero(new_keep) < 8:
            break
        if np.array_equal(new_keep, keep):
            keep = new_keep
            break
        keep = new_keep

    if bx is None or by is None or np.count_nonzero(keep) < 8:
        return None
    # Require genuine 2-D coverage.  A polynomial fitted to a narrow strip is unsafe.
    xr = float(np.ptp(cx[keep]) / max(width,1)); yr = float(np.ptp(cy[keep]) / max(height,1))
    if xr < 0.45 or yr < 0.45:
        return None
    predx = X @ bx; predy = X @ by
    err = np.hypot(predx-dx, predy-dy)
    return {
        'bx': bx, 'by': by, 'inliers': int(np.count_nonzero(keep)),
        'fit_median_px': float(np.median(err[keep])),
        'fit_p90_px': float(np.percentile(err[keep],90)),
        'coverage_x': xr, 'coverage_y': yr,
    }


def _poly_surface_rows(coeff, width, height, y0, y1):
    """Evaluate a quadratic surface in row chunks to keep RAM bounded."""
    xs = np.linspace(-1.0, 1.0, int(width), dtype=np.float32)
    # Use pixel-center normalized Y, matching the fit above closely enough for a
    # sub-pixel residual correction.
    yy = np.arange(int(y0), int(y1), dtype=np.float32)
    yn = (yy / max(float(height-1), 1.0)) * 2.0 - 1.0
    X = xs[None,:]
    Y = yn[:,None]
    c = np.asarray(coeff, dtype=np.float32)
    return (c[0] + c[1]*X + c[2]*Y + c[3]*X*Y + c[4]*X*X + c[5]*Y*Y).astype(np.float32, copy=False)


def _smooth_residual_remap(image, valid_mask, bx, by, max_warp=14.0, rows_per_chunk=256):
    """Apply a smooth residual displacement field in strips instead of huge full maps."""
    h, w = image.shape[:2]
    out = np.zeros_like(image)
    vm = valid_mask[:, :, 0] if getattr(valid_mask, 'ndim', 2) == 3 else valid_mask
    outv = np.zeros((h,w), np.uint8)
    base_x = np.arange(w, dtype=np.float32)[None,:]
    for y0 in range(0, h, int(rows_per_chunk)):
        y1 = min(h, y0 + int(rows_per_chunk))
        dx = _poly_surface_rows(bx, w, h, y0, y1)
        dy = _poly_surface_rows(by, w, h, y0, y1)
        # Never allow a local residual correction to become a large scene warp.
        dx = np.clip(dx, -float(max_warp), float(max_warp))
        dy = np.clip(dy, -float(max_warp), float(max_warp))
        base_y = np.arange(y0, y1, dtype=np.float32)[:,None]
        mapx = base_x + dx
        mapy = base_y + dy
        out[y0:y1] = cv2.remap(image, mapx, mapy, cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        outv[y0:y1] = cv2.remap(vm, mapx, mapy, cv2.INTER_NEAREST,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    return out, outv


def _smooth_mesh_residual_refine(reference, aligned, valid_mask, report_progress, max_passes=2):
    """Correct a slowly varying residual field after global registration.

    This stage is only used when global Homography/Affine/ECC is already close but the
    residual P90 is still large.  It uses high-confidence tile translations, robustly
    fits a quadratic displacement surface, and accepts a pass only if *fresh* tile
    measurements improve.  This is intentionally smoother and safer than dense optical
    flow for change detection because it cannot follow individual objects/buildings.
    """
    h, w = reference.shape[:2]
    current = aligned
    current_valid = valid_mask
    try:
        current_data_valid = common_data_valid_mask(reference, current, current_valid,
                                                    black_threshold=2, rows_per_chunk=768)
    except Exception:
        current_data_valid = current_valid

    def measure(img, dv):
        # Dense-enough grid to see edge residuals; 544 px tiles retain texture while
        # still localizing the spatially varying shift.
        return _phase_tile_metrics(reference, img, dv, grid=9, tile_size=544, max_shift=16.0)

    metrics = measure(current, current_data_valid)
    used = 0; points_used = 0; fit_med = float('inf')
    for pidx in range(max(1, int(max_passes))):
        if metrics['median_mag'] < 1.0 and metrics['p90_mag'] < 1.5:
            break
        shifts = list(metrics.get('shifts') or [])
        if len(shifts) < 8:
            break
        fit = _fit_smooth_residual_surface(shifts, w, h)
        if not fit:
            break
        bx, by = fit['bx'], fit['by']

        # Sanity-check the correction over a coarse lattice before applying it.
        gx = np.linspace(-1,1,7); gy=np.linspace(-1,1,7)
        XX,YY=np.meshgrid(gx,gy)
        D=_poly2_design(XX.ravel(),YY.ravel())
        pdx=D@bx; pdy=D@by; pmag=np.hypot(pdx,pdy)
        if not np.all(np.isfinite(pmag)) or float(np.max(pmag)) > 14.0:
            break

        test, test_valid = _smooth_residual_remap(current, current_valid, bx, by,
                                                  max_warp=14.0, rows_per_chunk=256)
        try:
            test_dv = common_data_valid_mask(reference, test, test_valid,
                                             black_threshold=2, rows_per_chunk=768)
        except Exception:
            test_dv = test_valid
        test_metrics = measure(test, test_dv)
        bm=float(metrics.get('median_mag',float('inf'))); bp=float(metrics.get('p90_mag',float('inf')))
        am=float(test_metrics.get('median_mag',float('inf'))); ap=float(test_metrics.get('p90_mag',float('inf')))
        bn=int(metrics.get('count',0)); an=int(test_metrics.get('count',0))
        if an < max(5, int(0.65*max(bn,1))) or not np.isfinite(am):
            break
        # The primary goal here is to cure the long residual tail seen by the user.
        score_before = bm + 0.38*bp
        score_after = am + 0.38*ap
        median_ok = am <= bm + 0.15
        tail_gain = bp - ap
        accept = median_ok and (score_after <= score_before - 0.18) and \
                 ((bm-am) >= 0.08 or tail_gain >= 0.45)
        if not accept:
            break

        current, current_valid, current_data_valid, metrics = test, test_valid, test_dv, test_metrics
        used += 1; points_used = max(points_used, int(fit['inliers'])); fit_med=float(fit['fit_median_px'])
        report_progress(90 + pidx*2,
                        f"Smooth Local Mesh {pidx+1}: median={am:.2f}px | P90={ap:.2f}px | points={fit['inliers']}")
        if am < 1.0 and ap < 1.5:
            break

    return current, current_valid, current_data_valid, metrics, used, points_used, fit_med

def align_advanced(reference, moving, preferred_ratio=0.72, reproj=3.0, progress=None):
    """High-accuracy automatic registration for large residual displacement.

    Pipeline:
      1) high-resolution proxy + SIFT ratio sweep,
      2) MAGSAC homography / stable affine model selection,
      3) second-pass residual SIFT refinement,
      4) conservative multi-scale ECC on gradient images,
      5) full-resolution tile phase-correlation check and final sub-pixel translation,
      6) hard quality gate: failed registration is NOT accepted for change detection.

    Returns aligned, H, report, valid_mask (same API as before).
    """
    def report_progress(value, text):
        if progress:
            progress(int(value), str(text))

    report_progress(4, 'تجهيز نسخة تحليل عالية الدقة للانزياح الكبير...')
    # 3600 px gives much better geometric precision than the old 2200 px proxy
    # while still avoiding full-resolution SIFT on very large rasters.
    moving_full, ref_p, mov_p, scale = _registration_proxy(reference, moving, max_dim=3600)
    hp, wp = ref_p.shape[:2]

    best, candidates = _estimate_initial_transform(
        ref_p, mov_p, scale, preferred_ratio, reproj, report_progress)
    H_proxy = best['H_proxy'].astype(np.float64)
    initial_reprojection_error = float(best['reprojection_error'])

    report_progress(51, 'تمت المواءمة الأولية؛ بدء تحسين الانزياح المتبقي...')
    H_proxy, residual_passes, residual_proxy_error, residual_inliers, residual_cov = _residual_sift_refine(
        ref_p, mov_p, H_proxy, report_progress, passes=2)

    report_progress(62, 'بدء ECC متعدد المستويات على الحواف والتفاصيل...')
    H_proxy, ecc_score, ecc_passes = _ecc_multiscale_refine(
        ref_p, mov_p, H_proxy, report_progress)

    # Convert proxy transform to full-resolution pixel coordinates.
    S = np.array([[scale,0,0],[0,scale,0],[0,0,1]], dtype=np.float64)
    H_full = np.linalg.inv(S) @ H_proxy @ S
    H_full /= H_full[2,2]
    h,w = reference.shape[:2]

    report_progress(78, 'تطبيق التحويل وفحص الإزاحة المتبقية على الدقة الأصلية...')
    aligned = _warp_rgb_full(moving_full, H_full, (w,h))
    valid_mask = cv2.warpPerspective(np.full(moving_full.shape[:2],255,np.uint8), H_full, (w,h),
                                     flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    # Also exclude true black/no-data borders from the geometric overlap metric.
    try:
        data_valid = common_data_valid_mask(reference, aligned, valid_mask, black_threshold=2, rows_per_chunk=768)
    except Exception:
        data_valid = valid_mask
    overlap = float(np.count_nonzero(data_valid) / max(data_valid.size,1))

    phase_before = _phase_tile_metrics(reference, aligned, data_valid, grid=4, tile_size=640, max_shift=18.0)
    final_translation_used = False
    # A small globally consistent residual shift can be corrected at true pixel scale.
    if phase_before['count'] >= 4:
        vec_mag = math.hypot(phase_before['median_dx'], phase_before['median_dy'])
        consistent = phase_before['mad'] <= max(0.85, 0.55 * max(phase_before['median_mag'],0.5))
        if 0.25 < vec_mag <= 10.0 and consistent:
            T = np.array([[1.0,0.0,-phase_before['median_dx']],
                          [0.0,1.0,-phase_before['median_dy']],
                          [0.0,0.0,1.0]], dtype=np.float64)
            H_test = T @ H_full
            aligned2 = _warp_rgb_full(moving_full, H_test, (w,h))
            valid2 = cv2.warpPerspective(np.full(moving_full.shape[:2],255,np.uint8), H_test, (w,h),
                                         flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            try:
                dv2 = common_data_valid_mask(reference, aligned2, valid2, black_threshold=2, rows_per_chunk=768)
            except Exception:
                dv2 = valid2
            phase2 = _phase_tile_metrics(reference, aligned2, dv2, grid=4, tile_size=640, max_shift=12.0)
            if phase2['count'] >= 3 and phase2['median_mag'] + 0.05 < phase_before['median_mag']:
                aligned = aligned2; valid_mask = valid2; data_valid = dv2
                H_full = H_test; phase_before = phase2
                overlap = float(np.count_nonzero(data_valid) / max(data_valid.size,1))
                final_translation_used = True
            else:
                del aligned2

    # Mandatory pre-gate Local Residual stage.  It is intentionally executed BEFORE
    # the hard quality rejection so a recoverable 2-10 px residual is corrected first.
    local_affine_passes = 0
    local_affine_inliers = 0
    if phase_before['count'] >= 3 and np.isfinite(phase_before['median_mag']) and \
            (phase_before['median_mag'] >= 1.0 or phase_before['p90_mag'] >= 1.5):
        report_progress(84, 'بدء Local Residual Alignment قبل بوابة الجودة النهائية...')
        aligned, H_full, valid_mask, data_valid, phase_before, local_affine_passes, local_affine_inliers = \
            _pre_gate_local_residual_refine(reference, moving_full, H_full, valid_mask, data_valid,
                                            report_progress, max_passes=3)
        overlap = float(np.count_nonzero(data_valid) / max(data_valid.size,1))

    # If a global projective/affine model is close in the center but still has a large
    # spatially varying tail, use a *smooth* local residual field before rejecting.
    # This is not dense optical flow: only a quadratic surface fitted to stable tiles is
    # allowed, so individual scene changes cannot be freely warped away.
    smooth_mesh_passes = 0
    smooth_mesh_points = 0
    smooth_mesh_fit_median = float('inf')
    if phase_before['count'] >= 5 and np.isfinite(phase_before['median_mag']) and \
            (float(phase_before['median_mag']) >= 1.0 or float(phase_before['p90_mag']) >= 1.5):
        report_progress(89, 'بدء Smooth Local Mesh لمعالجة الانزياح المتغير مكانيًا...')
        aligned2, valid2, dv2, phase2, mp, mpts, mfit = _smooth_mesh_residual_refine(
            reference, aligned, valid_mask, report_progress, max_passes=2)
        # Accept only what the mesh refiner itself validated on fresh tiles.
        if int(mp) > 0:
            aligned, valid_mask, data_valid, phase_before = aligned2, valid2, dv2, phase2
            smooth_mesh_passes, smooth_mesh_points, smooth_mesh_fit_median = int(mp), int(mpts), float(mfit)
            overlap = float(np.count_nonzero(data_valid) / max(data_valid.size,1))

    # Only now calculate the final residual and apply the hard quality gate.
    if phase_before['count'] >= 3 and np.isfinite(phase_before['median_mag']):
        final_error = float(phase_before['median_mag'])
        final_p90 = float(phase_before['p90_mag'])
        residual_tiles = int(phase_before['count'])
    elif residual_proxy_error is not None and np.isfinite(residual_proxy_error):
        final_error = float(residual_proxy_error / max(scale,1e-9))
        final_p90 = final_error * 1.8
        residual_tiles = 0
    else:
        final_error = float(best['reprojection_error'])
        final_p90 = final_error * 1.8
        residual_tiles = 0

    # Measure the final image independently. Identity reprojection error of fresh
    # SIFT correspondences measures remaining displacement, including mesh warps.
    final_proxy = cv2.resize(aligned, (wp, hp), interpolation=cv2.INTER_AREA)
    checked, _ = _estimate_initial_transform(ref_p, final_proxy, scale,
                                             preferred_ratio, reproj, lambda *_: None)
    reprojection_error = _reprojection_error(np.eye(3), checked['src'],
                                            checked['dst'], checked['mask']) / max(scale, 1e-9)
    rg, _ = _registration_gray_and_mask(ref_p)
    ag, _ = _registration_gray_and_mask(final_proxy)
    proxy_valid = cv2.resize(data_valid, (wp, hp), interpolation=cv2.INTER_NEAREST)
    ecc_score = _masked_corr(_ecc_feature(rg), _ecc_feature(ag), proxy_valid)
    best = dict(checked, ratio=best['ratio'], model=best['model'])
    report_progress(91, f'فحص الجودة النهائي: residual={final_error:.2f}px')
    # Hard quality gate requested for change detection: do not silently accept a visibly shifted result.
    if (not np.all(np.isfinite([final_error, final_p90, reprojection_error, ecc_score]))
            or residual_tiles < 4 or final_error >= 1.0 or final_p90 >= 1.5
            or reprojection_error >= 1.0 or ecc_score < 0.80
            or best['inlier_ratio'] < 0.50 or best['coverage'] < 0.18 or overlap < 0.55):
        raise RuntimeError(
            'تم رفض المواءمة النهائية لأن الانزياح المتبقي ما زال كبيرًا لكشف التغير الدقيق.\n'
            f'Residual median = {final_error:.2f} px | P90 = {final_p90:.2f} px | tiles = {residual_tiles}.\n'
            f'Local Residual passes = {local_affine_passes} | Smooth Mesh passes = {smooth_mesh_passes}.\n'
            f'Reprojection = {reprojection_error:.2f}px | ECC = {ecc_score:.4f} | '
            f'Inliers = {best["inlier_ratio"]:.1%} | Coverage = {best["coverage"]:.1%} | Overlap = {overlap:.1%}.\n'
            'لن يعتمد البرنامج هذه النتيجة حتى لا ينتج تغيّرات كاذبة.'
        )

    # Quality label now uses the measured final residual, not only the first SIFT fit.
    cov = float(best['coverage'])
    if final_error <= 1.0 and best['inlier_ratio'] >= .35 and cov >= .18 and overlap >= .55:
        label_ar,label_en='ممتاز','EXCELLENT'
    elif final_error <= 1.5 and best['inlier_ratio'] >= .25 and cov >= .12 and overlap >= .50:
        label_ar,label_en='جيد جدًا','VERY_GOOD'
    elif final_error <= 2.25 and best['inlier_ratio'] >= .20 and overlap >= .45:
        label_ar,label_en='مقبول','ACCEPTABLE'
    else:
        label_ar,label_en='ضعيف','WEAK'

    quality_score = 100.0 * np.clip(
        0.30*min(best['inlier_ratio']/0.50,1.0) +
        0.20*min(cov/0.28,1.0) +
        0.18*min(overlap/0.75,1.0) +
        0.32*max(0.0,1.0-final_error/2.5), 0, 1)

    report = {
        'matches': int(best['matches']), 'inliers': int(best['inliers']),
        'inlier_ratio': float(best['inlier_ratio']), 'selected_ratio': float(best['ratio']),
        'reprojection_error_px': float(reprojection_error),
        'residual_median_px': float(final_error),
        'alignment_approved': True,
        'initial_reprojection_error_px': initial_reprojection_error,
        'coverage_percent': float(cov*100.0),
        'overlap_percent': float(overlap*100.0),
        'ecc_score': None if ecc_score is None else float(ecc_score),
        'ecc_used': bool(ecc_passes > 0), 'ecc_passes': int(ecc_passes),
        'residual_sift_passes': int(residual_passes),
        'residual_phase_tiles': int(residual_tiles),
        'residual_p90_px': float(final_p90),
        'final_translation_refined': bool(final_translation_used),
        'local_affine_refined': bool(local_affine_passes > 0),
        'local_affine_passes': int(local_affine_passes),
        'local_affine_inliers': int(local_affine_inliers),
        'smooth_mesh_refined': bool(smooth_mesh_passes > 0),
        'smooth_mesh_passes': int(smooth_mesh_passes),
        'smooth_mesh_points': int(smooth_mesh_points),
        'smooth_mesh_fit_median_px': None if not np.isfinite(smooth_mesh_fit_median) else float(smooth_mesh_fit_median),
        'model': str(best.get('model','HOMOGRAPHY')),
        'quality_score': float(quality_score),
        'quality_ar': label_ar, 'quality_en': label_en,
        'proxy_scale': float(scale),
        'tested_ratios': [float(c['ratio']) for c in candidates],
    }
    report_progress(100, f'اكتملت المواءمة الدقيقة – {label_ar} – residual {final_error:.2f}px')
    return aligned, H_full, report, valid_mask

def align_sift(reference, moving, ratio=0.72, reproj=3.0):
    """Backward-compatible wrapper using the advanced registration engine."""
    aligned, H, report, _ = align_advanced(reference, moving, ratio, reproj)
    return aligned, H, report['matches'], report['inliers']

def alignment_quality(reference, aligned):
    g1 = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g2 = cv2.cvtColor(resize_like(reference, aligned), cv2.COLOR_RGB2GRAY).astype(np.float32)
    ecc = float(cv2.computeECC(g1, g2))
    return ecc



def _valid_bool_mask(valid_mask, shape):
    if valid_mask is None:
        return np.ones(shape[:2], dtype=bool)
    vm = valid_mask[:, :, 0] if getattr(valid_mask, 'ndim', 2) == 3 else valid_mask
    if vm.shape[:2] != shape[:2]:
        vm = cv2.resize(vm, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    return vm > 0


def common_data_valid_mask(reference, moving, base_valid_mask=None, black_threshold=2, rows_per_chunk=512):
    """Build a common valid-data mask for change analysis.

    Pixels are valid only when BOTH images contain non-black RGB data.  This is
    intended for GeoTIFFs that acquire an all-zero border after reprojection or
    geometric alignment.  An existing registration/overlap mask can be supplied
    with ``base_valid_mask`` and is intersected with the data-valid mask.

    The mask is built in row chunks so large rasters do not require additional
    full-resolution RGB temporary arrays.  The returned mask is uint8 (0/255),
    matching the rest of ChangeVision's valid-mask pipeline.
    """
    ref = np.asarray(reference)
    mov = np.asarray(moving)
    if ref.ndim not in (2, 3) or mov.ndim not in (2, 3):
        raise ValueError('أبعاد الصور غير مدعومة لإنشاء قناع NoData.')

    if mov.shape[:2] != ref.shape[:2]:
        mov = cv2.resize(mov, (ref.shape[1], ref.shape[0]), interpolation=cv2.INTER_LINEAR)

    h, w = ref.shape[:2]
    rows_per_chunk = max(64, int(rows_per_chunk))
    thr = int(np.clip(black_threshold, 0, 255))

    base = None
    if base_valid_mask is not None:
        base = base_valid_mask[:, :, 0] if getattr(base_valid_mask, 'ndim', 2) == 3 else base_valid_mask
        if base.shape[:2] != (h, w):
            base = cv2.resize(base, (w, h), interpolation=cv2.INTER_NEAREST)

    out = np.empty((h, w), dtype=np.uint8)
    for y0 in range(0, h, rows_per_chunk):
        y1 = min(h, y0 + rows_per_chunk)
        r = ref[y0:y1]
        m = mov[y0:y1]

        if r.ndim == 2:
            rv = r > thr
        else:
            rv = np.any(r[:, :, :min(3, r.shape[2])] > thr, axis=2)
        if m.ndim == 2:
            mv = m > thr
        else:
            mv = np.any(m[:, :, :min(3, m.shape[2])] > thr, axis=2)

        valid = rv & mv
        if base is not None:
            valid &= base[y0:y1] > 0
        out[y0:y1] = valid.astype(np.uint8) * 255

    return out

def _robust_channel_stats(channel, mask):
    vals = channel[mask]
    if vals.size < 32:
        vals = channel.reshape(-1)
    lo, hi = np.percentile(vals, (2.0, 98.0))
    vals = vals[(vals >= lo) & (vals <= hi)]
    if vals.size < 32:
        vals = channel[mask]
    mean = float(np.mean(vals)) if vals.size else 0.0
    std = float(np.std(vals)) if vals.size else 1.0
    return mean, max(std, 1.0)

def radiometric_match(reference, moving, valid_mask=None):
    """Robustly match moving image radiometry to reference.

    Uses LAB space: luminance is matched with robust mean/std and clipped, while
    chroma channels receive a conservative mean-shift. This reduces date/sensor
    brightness and color-cast differences without geometric warping.
    """
    moving = resize_like(reference, moving)
    ref = reference.astype(np.uint8, copy=False)
    mov = moving.astype(np.uint8, copy=False)
    vm = _valid_bool_mask(valid_mask, ref.shape)
    ref_lab = cv2.cvtColor(ref, cv2.COLOR_RGB2LAB).astype(np.float32)
    mov_lab = cv2.cvtColor(mov, cv2.COLOR_RGB2LAB).astype(np.float32)
    out = mov_lab.copy()
    # L channel: robust affine match with a conservative scale clamp.
    mr, sr = _robust_channel_stats(ref_lab[:, :, 0], vm)
    mm, sm = _robust_channel_stats(mov_lab[:, :, 0], vm)
    scale = float(np.clip(sr / max(sm, 1e-6), 0.65, 1.55))
    out[:, :, 0] = (mov_lab[:, :, 0] - mm) * scale + mr
    # Chroma: mean shift only; prevents aggressive recoloring.
    for c in (1, 2):
        mr_c, _ = _robust_channel_stats(ref_lab[:, :, c], vm)
        mm_c, _ = _robust_channel_stats(mov_lab[:, :, c], vm)
        shift = float(np.clip(mr_c - mm_c, -20.0, 20.0))
        out[:, :, c] = mov_lab[:, :, c] + shift
    out = np.clip(out, 0, 255).astype(np.uint8)
    rgb = cv2.cvtColor(out, cv2.COLOR_LAB2RGB)
    rgb[~vm] = 0
    return rgb

def _local_luminance_normalize(gray, sigma=11.0):
    g = gray.astype(np.float32) / 255.0
    mean = cv2.GaussianBlur(g, (0, 0), sigmaX=sigma, sigmaY=sigma)
    sq = cv2.GaussianBlur(g * g, (0, 0), sigmaX=sigma, sigmaY=sigma)
    std = np.sqrt(np.maximum(sq - mean * mean, 1e-5))
    z = (g - mean) / np.maximum(std, 0.08)
    return np.clip(z, -3.0, 3.0)

def edge_misregistration_mask(reference, moving, tolerance_px=3, valid_mask=None):
    """Mask edge pixels that have a matching edge within a small radius.

    Such thin paired edges are often caused by residual registration error rather
    than a real object appearing/disappearing. Pixels are suppressed only when an
    edge in one image has a nearby counterpart in the other image.
    """
    moving = resize_like(reference, moving)
    g1 = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(moving, cv2.COLOR_RGB2GRAY)
    # Small blur stabilizes Canny on high-resolution aerial imagery.
    g1 = cv2.GaussianBlur(g1, (3, 3), 0.8)
    g2 = cv2.GaussianBlur(g2, (3, 3), 0.8)
    e1 = cv2.Canny(g1, 55, 150) > 0
    e2 = cv2.Canny(g2, 55, 150) > 0
    tol = max(0, int(tolerance_px))
    if tol <= 0:
        paired = e1 & e2
    else:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*tol+1, 2*tol+1))
        d1 = cv2.dilate(e1.astype(np.uint8), k) > 0
        d2 = cv2.dilate(e2.astype(np.uint8), k) > 0
        paired = (e1 & d2) | (e2 & d1)
        # Dilate paired edges lightly so the full double-edge halo is covered.
        paired = cv2.dilate(paired.astype(np.uint8), np.ones((3,3), np.uint8)) > 0
    vm = _valid_bool_mask(valid_mask, reference.shape)
    return paired & vm

def robust_change_difference(a, b, valid_mask=None, edge_tolerance=3):
    """Legacy ROBUST score retained for backwards compatibility."""
    b = resize_like(a, b)
    vm = _valid_bool_mask(valid_mask, a.shape)
    b_match = radiometric_match(a, b, valid_mask)
    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2GRAY)
    z1 = _local_luminance_normalize(g1)
    z2 = _local_luminance_normalize(g2)
    local = np.clip(np.abs(z1 - z2) / 3.0, 0, 1)
    color = cv2.absdiff(a, b_match).astype(np.float32).mean(axis=2) / 255.0
    gx1 = cv2.Sobel(g1, cv2.CV_32F, 1, 0, ksize=3); gy1 = cv2.Sobel(g1, cv2.CV_32F, 0, 1, ksize=3)
    gx2 = cv2.Sobel(g2, cv2.CV_32F, 1, 0, ksize=3); gy2 = cv2.Sobel(g2, cv2.CV_32F, 0, 1, ksize=3)
    mag1 = cv2.magnitude(gx1, gy1); mag2 = cv2.magnitude(gx2, gy2)
    grad = np.clip(np.abs(mag1 - mag2) / 255.0, 0, 1)
    score = 0.52 * local + 0.30 * color + 0.18 * grad
    paired_edges = edge_misregistration_mask(a, b_match, edge_tolerance, valid_mask)
    score[paired_edges] *= 0.28
    score[~vm] = 0.0
    diff = np.clip(score * 255.0, 0, 255).astype(np.uint8)
    diff = cv2.medianBlur(diff, 3)
    diff[~vm] = 0
    return diff, b_match, paired_edges.astype(np.uint8) * 255


def _edge_uncertainty_zone(reference, moving, tolerance_px=3, valid_mask=None):
    """Return a conservative band where residual registration error is plausible.

    A pixel enters the zone only when an edge from one date has an edge from the
    other date within the requested tolerance.  The band is widened slightly to
    cover the common double-edge halo produced by 1-5 px residual misregistration.
    """
    moving = resize_like(reference, moving)
    g1 = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(moving, cv2.COLOR_RGB2GRAY)
    g1 = cv2.GaussianBlur(g1, (3, 3), 0.9)
    g2 = cv2.GaussianBlur(g2, (3, 3), 0.9)
    e1 = cv2.Canny(g1, 45, 135) > 0
    e2 = cv2.Canny(g2, 45, 135) > 0
    tol = max(0, int(tolerance_px))
    r = max(1, tol)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*r+1, 2*r+1))
    d1 = cv2.dilate(e1.astype(np.uint8), k) > 0
    d2 = cv2.dilate(e2.astype(np.uint8), k) > 0
    paired = (e1 & d2) | (e2 & d1)
    band_r = max(1, min(8, tol + 2))
    kb = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*band_r+1, 2*band_r+1))
    zone = cv2.dilate(paired.astype(np.uint8), kb) > 0
    return zone & _valid_bool_mask(valid_mask, reference.shape)


def _multiscale_interior_evidence(g1, g2):
    """Low-frequency evidence that favors filled/area changes over thin edge halos."""
    a = g1.astype(np.float32) / 255.0
    b = g2.astype(np.float32) / 255.0
    d2 = np.abs(cv2.GaussianBlur(a, (0, 0), 2.0) - cv2.GaussianBlur(b, (0, 0), 2.0))
    d5 = np.abs(cv2.GaussianBlur(a, (0, 0), 5.0) - cv2.GaussianBlur(b, (0, 0), 5.0))
    return np.clip(0.65*d2 + 0.35*d5, 0.0, 1.0)


def robust_change_difference_v2(a, b, valid_mask=None, edge_tolerance=3):
    """ROBUST V2 change score tuned for high-resolution aerial imagery.

    The score combines radiometric matching, local contrast normalization,
    chromatic residuals and multi-scale *interior* evidence.  A confidence-aware
    edge uncertainty band strongly suppresses thin double-edge artifacts only when
    there is little low-frequency evidence of a real area change.  Strong interior
    changes remain visible even when they intersect pre-existing edges.
    """
    b = resize_like(a, b)
    vm = _valid_bool_mask(valid_mask, a.shape)
    b_match = radiometric_match(a, b, valid_mask)

    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2GRAY)
    z1 = _local_luminance_normalize(g1, sigma=15.0)
    z2 = _local_luminance_normalize(g2, sigma=15.0)
    local = np.clip(np.abs(z1-z2) / 3.2, 0.0, 1.0)

    # Robust color residual in LAB; luminance is deliberately weighted less than chroma.
    lab1 = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2LAB).astype(np.float32)
    dl = np.abs(lab1[:,:,0]-lab2[:,:,0]) / 255.0
    da = np.abs(lab1[:,:,1]-lab2[:,:,1]) / 128.0
    db = np.abs(lab1[:,:,2]-lab2[:,:,2]) / 128.0
    color = np.clip(0.45*dl + 0.275*da + 0.275*db, 0.0, 1.0)

    interior = _multiscale_interior_evidence(g1, g2)

    # Gradient disagreement is useful, but receives a low weight because it is
    # exactly where small registration errors are most visible.
    gx1=cv2.Sobel(g1,cv2.CV_32F,1,0,ksize=3); gy1=cv2.Sobel(g1,cv2.CV_32F,0,1,ksize=3)
    gx2=cv2.Sobel(g2,cv2.CV_32F,1,0,ksize=3); gy2=cv2.Sobel(g2,cv2.CV_32F,0,1,ksize=3)
    grad=np.clip(np.abs(cv2.magnitude(gx1,gy1)-cv2.magnitude(gx2,gy2))/320.0,0.0,1.0)

    score = 0.34*local + 0.27*color + 0.31*interior + 0.08*grad
    zone = _edge_uncertainty_zone(a, b_match, edge_tolerance, valid_mask)

    # Confidence-aware suppression. Thin-edge disagreement with weak interior
    # support is heavily attenuated; substantial interior change is only mildly
    # attenuated so new/removed objects are preserved.
    weak = zone & (interior < 0.075) & (color < 0.16)
    medium = zone & ~weak & (interior < 0.16)
    score[weak] *= 0.10
    score[medium] *= 0.48

    # Remove isolated high-frequency sparkle without blurring actual change regions.
    score[~vm] = 0.0
    diff = np.clip(score*255.0,0,255).astype(np.uint8)
    diff = cv2.medianBlur(diff,3)
    diff[~vm]=0
    return diff, b_match, zone.astype(np.uint8)*255


def _local_correlation_change(g1, g2, radius=5):
    """Fast local structural disagreement score (0..1).

    Uses box-filtered normalized cross-correlation.  It is deliberately local:
    stable texture remains low even after moderate radiometric differences, while
    genuinely changed neighborhoods rise.
    """
    a = g1.astype(np.float32) / 255.0
    b = g2.astype(np.float32) / 255.0
    k = 2 * max(1, int(radius)) + 1
    size = (k, k)
    ma = cv2.boxFilter(a, cv2.CV_32F, size, normalize=True)
    mb = cv2.boxFilter(b, cv2.CV_32F, size, normalize=True)
    maa = cv2.boxFilter(a*a, cv2.CV_32F, size, normalize=True)
    mbb = cv2.boxFilter(b*b, cv2.CV_32F, size, normalize=True)
    mab = cv2.boxFilter(a*b, cv2.CV_32F, size, normalize=True)
    va = np.maximum(maa - ma*ma, 1e-5)
    vb = np.maximum(mbb - mb*mb, 1e-5)
    cov = mab - ma*mb
    corr = cov / np.sqrt(va*vb + 1e-6)
    corr = np.clip(corr, -1.0, 1.0)
    # Positive local correlation means structurally stable. Negative correlation
    # is treated as strong change.
    return np.clip((1.0 - corr) * 0.5, 0.0, 1.0)


def _texture_energy(gray):
    g = gray.astype(np.float32) / 255.0
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    return cv2.GaussianBlur(mag, (0, 0), 1.2)


def robust_change_difference_v3(a, b, valid_mask=None, edge_tolerance=3):
    """ROBUST V3 change score.

    V3 keeps V2 radiometric matching but adds local structural correlation,
    texture-aware edge confidence and stronger paired-edge halo suppression.
    It is designed to reduce the many thin false components produced by residual
    1-5 px registration error on high-resolution aerial imagery.
    """
    b = resize_like(a, b)
    vm = _valid_bool_mask(valid_mask, a.shape)
    b_match = radiometric_match(a, b, valid_mask)

    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2GRAY)

    # Local illumination invariant residual.
    z1 = _local_luminance_normalize(g1, sigma=17.0)
    z2 = _local_luminance_normalize(g2, sigma=17.0)
    local = np.clip(np.abs(z1-z2) / 3.4, 0.0, 1.0)

    # Structural neighborhood disagreement is computed on mildly blurred images.
    # This keeps it useful as supporting evidence without making a 2-4 px residual
    # shift look like a scene-wide change.
    s1 = cv2.GaussianBlur(g1, (0, 0), 2.2)
    s2 = cv2.GaussianBlur(g2, (0, 0), 2.2)
    structure = _local_correlation_change(s1, s2, radius=7)

    lab1 = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2LAB).astype(np.float32)
    dl = np.abs(lab1[:,:,0]-lab2[:,:,0]) / 255.0
    da = np.abs(lab1[:,:,1]-lab2[:,:,1]) / 128.0
    db = np.abs(lab1[:,:,2]-lab2[:,:,2]) / 128.0
    color = np.clip(0.38*dl + 0.31*da + 0.31*db, 0.0, 1.0)

    # Filled/area support at multiple scales is the strongest evidence in V3.
    interior = _multiscale_interior_evidence(g1, g2)

    # Main score deliberately avoids giving edges a large direct weight.
    score = 0.30*local + 0.22*color + 0.40*interior + 0.08*structure

    zone = _edge_uncertainty_zone(a, b_match, edge_tolerance, valid_mask)

    # Strong V3 suppression: matched-edge halos are discounted unless independent
    # low-frequency/color/structural evidence supports a real area change.
    support = 0.55*interior + 0.30*color + 0.15*structure
    very_weak = zone & (support < 0.08)
    weak = zone & (support >= 0.08) & (support < 0.15)
    medium = zone & (support >= 0.15) & (support < 0.24)
    score[very_weak] *= 0.03
    score[weak] *= 0.12
    score[medium] *= 0.45

    # Suppress isolated radiometric sparkle when all three independent cues agree
    # that the neighborhood is stable.
    sparkle = (interior < 0.04) & (color < 0.08) & (local < 0.10)
    score[sparkle] *= 0.15

    score[~vm] = 0.0
    diff = np.clip(score*255.0, 0, 255).astype(np.uint8)
    diff = cv2.medianBlur(diff, 3)
    diff[~vm] = 0
    return diff, b_match, zone.astype(np.uint8)*255


def change_mask_v3(diff_gray, threshold=35, min_area=80, kernel=3, valid_mask=None, edge_tolerance=3):
    """Confidence/hysteresis mask for ROBUST V3.

    Weak pixels are retained only when they belong to a component with stronger
    evidence or sufficient spatial support.  This sharply reduces thousands of
    tiny threshold fragments without simply raising the global threshold.
    """
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    d = diff_gray.astype(np.uint8, copy=False)
    thr = int(np.clip(threshold, 0, 255))
    min_area = max(1, int(min_area))
    kernel = max(1, int(kernel))
    if kernel % 2 == 0:
        kernel += 1

    vm = _valid_bool_mask(valid_mask, d.shape)
    weak = (d >= thr) & vm
    # Strong seed threshold scales gently with the user's threshold.
    strong_thr = int(min(255, thr + max(16, round(0.55*thr))))
    strong = (d >= strong_thr) & vm

    # Clean only isolated one-pixel sparkle before component reasoning.
    weak_u8 = weak.astype(np.uint8)*255
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3))
    weak_u8 = cv2.morphologyEx(weak_u8, cv2.MORPH_OPEN, k3)

    n, labels, st, _ = cv2.connectedComponentsWithStats(weak_u8, 8)
    clean = np.zeros_like(d, dtype=np.uint8)
    tol = max(0, int(edge_tolerance))

    for i in range(1, n):
        area = int(st[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x, y, w, h = map(int, st[i, :4])
        comp = labels == i
        vals = d[comp]
        mean_v = float(vals.mean()) if vals.size else 0.0
        max_v = int(vals.max()) if vals.size else 0
        strong_count = int(np.count_nonzero(strong & comp))
        strong_frac = strong_count / max(area, 1)
        fill = area / max(float(w*h), 1.0)
        thin = min(w, h) <= max(3, 2*tol + 1)

        # Keep large or strong components, while requiring more evidence from thin
        # low-confidence fragments typical of misregistration halos.
        large = area >= max(min_area*8, 1200)
        confident = (mean_v >= thr + 10) or (strong_frac >= 0.018) or (max_v >= strong_thr + 24)
        compact_support = fill >= 0.10 and mean_v >= thr + 5
        if thin and not large and mean_v < thr + 18 and strong_frac < 0.03:
            continue
        if large or confident or compact_support:
            clean[comp] = 255

    # Final gentle morphology. Opening removes needle-like remnants; closing joins
    # nearby evidence belonging to the same changed object.
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN, k)
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, k)
    clean[~vm] = 0
    return clean


def _shift_rgb(img, dx, dy):
    """Translate RGB image by integer pixels with reflected borders."""
    h, w = img.shape[:2]
    M = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT101)


def _local_shift_tolerant_residual(reference, moving, tolerance_px=3, valid_mask=None):
    """Approximate residual after allowing a tiny local translation.

    This is intentionally computed on luminance and at a reduced set of shifts.
    It removes double-edge halos from residual 1-5 px registration error without
    performing another global warp.  A changed filled object still has no good
    nearby match and therefore keeps a high residual.
    """
    moving = resize_like(reference, moving)
    g1 = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(moving, cv2.COLOR_RGB2GRAY)
    g1 = cv2.GaussianBlur(g1, (0,0), 1.6).astype(np.float32)
    g2 = cv2.GaussianBlur(g2, (0,0), 1.6).astype(np.float32)
    tol = max(0, int(tolerance_px))
    # Sparse but symmetric displacement set keeps the full-resolution operation practical.
    steps = {0}
    if tol > 0:
        steps.update({-tol, tol})
        if tol >= 3:
            m = max(1, tol//2); steps.update({-m, m})
    shifts = {(0,0)}
    for d in steps:
        shifts.add((d,0)); shifts.add((0,d))
    if tol >= 2:
        shifts.update({(-tol,-tol),(-tol,tol),(tol,-tol),(tol,tol)})
    best = np.full(g1.shape, 255.0, np.float32)
    for dx,dy in shifts:
        M = np.float32([[1,0,dx],[0,1,dy]])
        sh = cv2.warpAffine(g2, M, (g2.shape[1], g2.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT101)
        d = cv2.absdiff(g1, sh)
        np.minimum(best, d, out=best)
    vm = _valid_bool_mask(valid_mask, reference.shape)
    best[~vm] = 0.0
    return np.clip(best / 80.0, 0.0, 1.0)


def robust_change_difference_v4(a, b, valid_mask=None, edge_tolerance=3):
    """ROBUST V4 object-centric score for high-resolution aerial imagery.

    V4 combines radiometric matching, local illumination normalization,
    multi-scale area evidence and a local shift-tolerant residual.  Edge-only
    disagreement is heavily discounted unless a filled neighborhood also changes.
    """
    b = resize_like(a, b)
    vm = _valid_bool_mask(valid_mask, a.shape)
    b_match = radiometric_match(a, b, valid_mask)
    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b_match, cv2.COLOR_RGB2GRAY)

    z1 = _local_luminance_normalize(g1, sigma=19.0)
    z2 = _local_luminance_normalize(g2, sigma=19.0)
    local = np.clip(np.abs(z1-z2)/3.6, 0.0, 1.0)

    # Area evidence at three scales: true object change persists as scale grows.
    f1 = g1.astype(np.float32)/255.0; f2 = g2.astype(np.float32)/255.0
    d2 = np.abs(cv2.GaussianBlur(f1,(0,0),2.0)-cv2.GaussianBlur(f2,(0,0),2.0))
    d5 = np.abs(cv2.GaussianBlur(f1,(0,0),5.0)-cv2.GaussianBlur(f2,(0,0),5.0))
    d9 = np.abs(cv2.GaussianBlur(f1,(0,0),9.0)-cv2.GaussianBlur(f2,(0,0),9.0))
    persistent = np.clip(0.40*d2 + 0.35*d5 + 0.25*d9, 0.0, 1.0)

    # Structural disagreement on a wider neighborhood.
    structure = _local_correlation_change(cv2.GaussianBlur(g1,(0,0),2.8), cv2.GaussianBlur(g2,(0,0),2.8), radius=9)

    lab1=cv2.cvtColor(a,cv2.COLOR_RGB2LAB).astype(np.float32)
    lab2=cv2.cvtColor(b_match,cv2.COLOR_RGB2LAB).astype(np.float32)
    dl=np.abs(lab1[:,:,0]-lab2[:,:,0])/255.0
    da=np.abs(lab1[:,:,1]-lab2[:,:,1])/128.0
    db=np.abs(lab1[:,:,2]-lab2[:,:,2])/128.0
    color=np.clip(0.30*dl+0.35*da+0.35*db,0.0,1.0)

    shift_res = _local_shift_tolerant_residual(a, b_match, edge_tolerance, valid_mask)
    score = 0.20*local + 0.34*persistent + 0.18*structure + 0.16*color + 0.12*shift_res

    zone = _edge_uncertainty_zone(a, b_match, edge_tolerance, valid_mask)
    support = 0.52*persistent + 0.22*shift_res + 0.16*color + 0.10*structure
    # Very strong edge suppression unless independent filled-area evidence exists.
    score[zone & (support < 0.075)] *= 0.02
    score[zone & (support >= 0.075) & (support < 0.14)] *= 0.10
    score[zone & (support >= 0.14) & (support < 0.23)] *= 0.38

    stable = (persistent < 0.035) & (shift_res < 0.08) & (color < 0.07)
    score[stable] *= 0.08
    score[~vm]=0.0
    diff=np.clip(score*255.0,0,255).astype(np.uint8)
    diff=cv2.medianBlur(diff,3)
    diff[~vm]=0
    return diff, b_match, zone.astype(np.uint8)*255


def change_mask_v4(diff_gray, threshold=35, min_area=80, kernel=3, valid_mask=None, edge_tolerance=3):
    """Object-centric V4 mask.

    Uses an adaptive noise floor, strong-core hysteresis and component thickness/
    compactness tests.  Thin line-like halos are rejected even when long, while
    filled changed objects are preserved and nearby fragments are merged gently.
    """
    if diff_gray.ndim==3:
        diff_gray=cv2.cvtColor(diff_gray,cv2.COLOR_RGB2GRAY)
    d=diff_gray.astype(np.uint8,copy=False)
    vm=_valid_bool_mask(valid_mask,d.shape)
    vals=d[vm]
    user_thr=int(np.clip(threshold,0,255))
    if vals.size:
        med=float(np.median(vals)); mad=float(np.median(np.abs(vals-med)))
        noise_thr=int(np.clip(round(med + 4.2*1.4826*mad), 0, 255))
    else:
        noise_thr=user_thr
    thr=max(user_thr, noise_thr)
    # Do not let adaptation silently jump absurdly far above the user's intent.
    thr=min(thr, min(255,user_thr+32))
    strong_thr=min(255, thr + max(16, int(round(0.45*thr))))

    cand=((d>=thr)&vm).astype(np.uint8)*255
    # Remove single-pixel sparkle but do not close edge ribbons before shape tests.
    cand=cv2.morphologyEx(cand,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3)))
    dist=cv2.distanceTransform((cand>0).astype(np.uint8),cv2.DIST_L2,5)
    n,labels,st,_=cv2.connectedComponentsWithStats(cand,8)
    clean=np.zeros_like(cand)
    min_area=max(1,int(min_area)); tol=max(1,int(edge_tolerance))
    core_req=max(1.7,0.72*tol)

    for i in range(1,n):
        area=int(st[i,cv2.CC_STAT_AREA])
        if area<min_area: continue
        x,y,w,h=map(int,st[i,:4]); comp=labels==i
        vv=d[comp]
        if not vv.size: continue
        mean=float(vv.mean()); p90=float(np.percentile(vv,90)); mx=float(vv.max())
        strong_frac=float(np.count_nonzero(vv>=strong_thr))/max(area,1)
        fill=area/max(float(w*h),1.0)
        depth=float(dist[comp].max()) if np.any(comp) else 0.0
        aspect=max(w,h)/max(min(w,h),1)
        # A long thin component with no interior core is the signature of a shifted edge.
        line_like=(depth<core_req and (fill<0.16 or aspect>6.0))
        huge=area>=max(5000,min_area*15)
        intensity=(0.45*mean+0.40*p90+0.15*mx)/255.0
        core_score=min(depth/max(core_req*2.2,1e-6),1.0)
        fill_score=min(fill/0.28,1.0)
        area_score=min(np.log1p(area)/np.log1p(max(6000,min_area*20)),1.0)
        conf=0.46*intensity+0.27*core_score+0.15*fill_score+0.12*area_score
        if line_like and not (huge and conf>=0.58):
            continue
        if strong_frac<0.004 and p90 < thr+10 and conf<0.52:
            continue
        if conf>=0.40 or huge:
            clean[comp]=255

    # Merge fragments of the same object only over a tiny distance.
    ksz=max(3,int(kernel)); ksz += (ksz%2==0)
    k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(ksz,ksz))
    clean=cv2.morphologyEx(clean,cv2.MORPH_CLOSE,k)
    clean=cv2.morphologyEx(clean,cv2.MORPH_OPEN,k)
    clean[~vm]=0
    return clean


def robust_change_difference_v5(a, b, valid_mask=None, edge_tolerance=3):
    """ROBUST V5 object-interior score.

    V5 deliberately builds on the proven V4 radiometric/registration-tolerant
    score, then asks a second question: does the response occupy a coherent
    neighbourhood, or is it only a thin displaced edge?  This reduces the
    thousands of one-pixel/edge fragments that remain on very large aerial
    images without simply raising the user's Threshold.
    """
    base, matched, edge_zone = robust_change_difference_v4(
        a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance
    )
    vm = _valid_bool_mask(valid_mask, base.shape)
    d = base.astype(np.float32) / 255.0

    # True object changes persist inside a neighbourhood.  Misregistration
    # halos are usually narrow and disappear rapidly under area averaging.
    a3 = cv2.GaussianBlur(d, (0, 0), 2.2)
    a7 = cv2.GaussianBlur(d, (0, 0), 5.0)
    occupancy = cv2.boxFilter((d > 0.10).astype(np.float32), -1, (9, 9),
                              normalize=True, borderType=cv2.BORDER_REFLECT101)

    score = 0.48*d + 0.32*a3 + 0.20*a7
    edge = edge_zone > 0

    # Sparse line response: strong penalty unless there is broader interior
    # support.  This is the main V5 edge-halo rejection stage.
    sparse = occupancy < 0.17
    score[sparse & (a7 < 0.075)] *= 0.12
    score[sparse & (a7 >= 0.075) & (a7 < 0.13)] *= 0.35

    # Inside the registration uncertainty zone require even stronger area
    # support; otherwise keep only a small fraction of the edge response.
    score[edge & (a7 < 0.070)] *= 0.04
    score[edge & (a7 >= 0.070) & (a7 < 0.12)] *= 0.18
    score[edge & (a7 >= 0.12) & (occupancy < 0.24)] *= 0.55

    # Very weak responses are noise, but preserve strong coherent cores.
    score[(d < 0.055) & (a7 < 0.050)] *= 0.05
    score[~vm] = 0.0
    out = np.clip(score * 255.0, 0, 255).astype(np.uint8)
    out = cv2.medianBlur(out, 3)
    out[~vm] = 0
    return out, matched, edge_zone


def change_mask_v5(diff_gray, threshold=35, min_area=80, kernel=3, valid_mask=None, edge_tolerance=3):
    """Final V5 object mask with hysteresis and shape confidence.

    A region must have a strong interior core or sufficiently compact filled
    support. Long thin ribbons typical of displaced roads/building edges are
    rejected. Threshold remains user-controlled but is protected by a robust
    noise floor estimated from the valid overlap.
    """
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    d = diff_gray.astype(np.uint8, copy=False)
    vm = _valid_bool_mask(valid_mask, d.shape)
    vals = d[vm]
    user_thr = int(np.clip(threshold, 0, 255))

    if vals.size:
        med = float(np.median(vals))
        mad = float(np.median(np.abs(vals - med)))
        # Slightly more conservative than V4, but capped so the GUI value
        # still has clear meaning.
        noise_thr = int(np.clip(round(med + 4.8 * 1.4826 * mad), 0, 255))
    else:
        noise_thr = user_thr
    thr = min(max(user_thr, noise_thr), min(255, user_thr + 28))
    strong_thr = min(255, thr + max(18, int(round(0.50 * thr))))

    weak = ((d >= thr) & vm).astype(np.uint8) * 255
    strong = ((d >= strong_thr) & vm).astype(np.uint8) * 255
    weak = cv2.morphologyEx(weak, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3)))

    # Distance-to-background is a robust measure of real object interior
    # thickness. Edge halos rarely have a deep core.
    dist = cv2.distanceTransform((weak > 0).astype(np.uint8), cv2.DIST_L2, 5)
    n, labels, st, _ = cv2.connectedComponentsWithStats(weak, 8)
    clean = np.zeros_like(weak)
    min_area = max(1, int(min_area))
    tol = max(1, int(edge_tolerance))
    core_req = max(2.0, 0.90 * tol)

    for i in range(1, n):
        area = int(st[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x, y, w, h = map(int, st[i, :4])
        comp = labels == i
        vv = d[comp]
        if not vv.size:
            continue

        mean = float(vv.mean())
        p75 = float(np.percentile(vv, 75))
        p90 = float(np.percentile(vv, 90))
        mx = float(vv.max())
        strong_frac = float(np.count_nonzero(strong[comp])) / max(area, 1)
        fill = area / max(float(w*h), 1.0)
        depth = float(dist[comp].max()) if np.any(comp) else 0.0
        aspect = max(w, h) / max(min(w, h), 1)

        # Perimeter/compactness on the component ROI.
        roi = (labels[y:y+h, x:x+w] == i).astype(np.uint8) * 255
        contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        per = sum(cv2.arcLength(c, True) for c in contours) if contours else 0.0
        compact = float(4.0 * np.pi * area / max(per*per, 1e-6))

        intensity = (0.30*mean + 0.25*p75 + 0.30*p90 + 0.15*mx) / 255.0
        core_score = min(depth / max(core_req*2.0, 1e-6), 1.0)
        fill_score = min(fill / 0.34, 1.0)
        compact_score = min(compact / 0.18, 1.0)
        area_score = min(np.log1p(area) / np.log1p(max(8000, min_area*25)), 1.0)
        conf = (0.40*intensity + 0.24*core_score + 0.14*fill_score +
                0.10*compact_score + 0.12*area_score)

        line_like = (depth < core_req and (fill < 0.20 or aspect > 5.5 or compact < 0.035))
        tiny_core = depth < 1.6 and strong_frac < 0.010
        huge = area >= max(9000, min_area*24)

        if line_like and not (huge and conf >= 0.62):
            continue
        if tiny_core and conf < 0.58:
            continue
        if strong_frac < 0.006 and p90 < thr + 12 and conf < 0.56:
            continue
        if conf >= 0.46 or (huge and conf >= 0.40):
            clean[comp] = 255

    # Merge only fragments that are very close; do not bridge unrelated roads.
    ksz = max(3, int(kernel)); ksz += (ksz % 2 == 0)
    close_sz = min(7, max(3, ksz + 2))
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (close_sz, close_sz)))
    clean = cv2.morphologyEx(clean, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksz, ksz)))

    # One final area pass after merging/opening.
    n2, lab2, st2, _ = cv2.connectedComponentsWithStats(clean, 8)
    final = np.zeros_like(clean)
    for i in range(1, n2):
        if int(st2[i, cv2.CC_STAT_AREA]) >= min_area:
            final[lab2 == i] = 255
    final[~vm] = 0
    return final


def robust_change_difference_v6(a, b, valid_mask=None, edge_tolerance=3):
    """ROBUST V6: object/region change score with illumination and edge rejection.

    Combines the V5 registration-tolerant response with coarse-scale LAB
    appearance change. Thin high-frequency edge residuals are deliberately
    suppressed unless supported by a filled neighbourhood at multiple scales.
    """
    base, matched, edge_zone = robust_change_difference_v5(
        a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance
    )
    vm = _valid_bool_mask(valid_mask, base.shape)
    ref = a.astype(np.uint8)
    mov = matched.astype(np.uint8)
    lab1 = cv2.cvtColor(ref, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab2 = cv2.cvtColor(mov, cv2.COLOR_RGB2LAB).astype(np.float32)
    # Coarse appearance suppresses pixel/texture differences and shadows.
    l1 = cv2.GaussianBlur(lab1[:,:,0], (0,0), 3.2)
    l2 = cv2.GaussianBlur(lab2[:,:,0], (0,0), 3.2)
    c1a = cv2.GaussianBlur(lab1[:,:,1], (0,0), 2.2); c2a = cv2.GaussianBlur(lab2[:,:,1], (0,0), 2.2)
    c1b = cv2.GaussianBlur(lab1[:,:,2], (0,0), 2.2); c2b = cv2.GaussianBlur(lab2[:,:,2], (0,0), 2.2)
    lum = np.abs(l1-l2) / 255.0
    chroma = np.sqrt((c1a-c2a)**2 + (c1b-c2b)**2) / 181.0
    d = base.astype(np.float32)/255.0
    coarse = cv2.GaussianBlur(d, (0,0), 4.5)
    occ = cv2.boxFilter((d > 0.085).astype(np.float32), -1, (15,15), normalize=True)
    # Region support dominates; luminance alone is deliberately weak.
    support = np.maximum(coarse, 0.55*chroma + 0.20*lum)
    score = 0.28*d + 0.46*coarse + 0.20*chroma + 0.06*lum
    # Kill thin contours and residual registration halos.
    score[(occ < 0.16) & (support < 0.12)] *= 0.08
    score[(occ < 0.24) & (support < 0.18)] *= 0.30
    ez = edge_zone > 0
    score[ez & (occ < 0.28) & (chroma < 0.16)] *= 0.12
    score[(chroma < 0.045) & (coarse < 0.075)] *= 0.12
    score[~vm] = 0
    out=np.clip(score*255.0,0,255).astype(np.uint8)
    out=cv2.medianBlur(out,5); out[~vm]=0
    return out, matched, edge_zone


def change_mask_v6(diff_gray, threshold=35, min_area=100, kernel=3, valid_mask=None, edge_tolerance=3):
    """V6 region-first mask: retain filled coherent changes, reject linework."""
    if diff_gray.ndim == 3:
        diff_gray=cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    d=diff_gray.astype(np.uint8, copy=False)
    vm=_valid_bool_mask(valid_mask,d.shape)
    vals=d[vm]
    user=int(np.clip(threshold,0,255))
    if vals.size:
        med=float(np.median(vals)); mad=float(np.median(np.abs(vals-med)))
        nf=int(np.clip(round(med+5.5*1.4826*mad),0,255))
    else: nf=user
    thr=min(max(user,nf), min(255,user+32))
    raw=((d>=thr)&vm).astype(np.uint8)*255
    raw=cv2.morphologyEx(raw,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3)))
    # Require spatially filled support, not merely connected edge traces.
    support=cv2.boxFilter((raw>0).astype(np.float32),-1,(11,11),normalize=True)
    seed=((raw>0)&(support>=0.16)).astype(np.uint8)*255
    seed=cv2.morphologyEx(seed,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5)))
    n,lab,st,_=cv2.connectedComponentsWithStats(seed,8)
    out=np.zeros_like(seed); min_area=max(1,int(min_area)); tol=max(1,int(edge_tolerance))
    dist=cv2.distanceTransform((seed>0).astype(np.uint8),cv2.DIST_L2,5)
    for i in range(1,n):
        area=int(st[i,cv2.CC_STAT_AREA])
        if area < min_area: continue
        x,y,w,h=map(int,st[i,:4]); comp=lab==i
        depth=float(dist[comp].max()); fill=area/max(float(w*h),1.0); aspect=max(w,h)/max(min(w,h),1)
        vv=d[comp]; p80=float(np.percentile(vv,80)); mean=float(vv.mean())
        roi=(lab[y:y+h,x:x+w]==i).astype(np.uint8)*255
        cs,_=cv2.findContours(roi,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        per=sum(cv2.arcLength(c,True) for c in cs) if cs else 0.0
        compact=float(4*np.pi*area/max(per*per,1e-6))
        line=(depth < max(2.2,0.75*tol) and (fill<0.18 or aspect>6.5 or compact<0.028))
        weak=(p80 < thr+10 and mean < thr+5)
        if line: continue
        if weak and depth < 3.0: continue
        out[comp]=255
    k=max(3,int(kernel)); k += (k%2==0)
    out=cv2.morphologyEx(out,cv2.MORPH_CLOSE,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(min(7,k+2),)*2))
    n,lab,st,_=cv2.connectedComponentsWithStats(out,8); final=np.zeros_like(out)
    for i in range(1,n):
        if int(st[i,cv2.CC_STAT_AREA])>=min_area: final[lab==i]=255
    final[~vm]=0
    return final


def postfilter_v6_candidates(candidate_mask, diff_gray, reference, moving, image_data=None,
                             valid_mask=None, edge_tolerance=3, accept_confidence=60,
                             review_confidence=45):
    """Conservative post-filter applied *after* ROBUST V6.

    ROBUST V6 itself is left untouched.  This stage classifies each V6 connected
    component using region thickness/fill, V6 evidence strength and whether the
    candidate mostly lies on strong edges that are already present in BOTH
    aligned images.  Only high-confidence components enter the final Change Map.

    Returns: final_mask, display_rows, summary
      - final_mask: accepted components only
      - display_rows: accepted + review components with confidence/classification
      - summary: candidate/accepted/review/rejected counts
    """
    if candidate_mask.ndim == 3:
        candidate_mask = cv2.cvtColor(candidate_mask, cv2.COLOR_RGB2GRAY)
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    mask = (candidate_mask > 0).astype(np.uint8)
    d = diff_gray.astype(np.uint8, copy=False)
    h, w = mask.shape
    ref = resize_like(np.empty((h,w,3), dtype=np.uint8), reference) if reference.shape[:2] != (h,w) else reference
    mov = resize_like(ref, moving)
    g1 = cv2.cvtColor(ref, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(mov, cv2.COLOR_RGB2GRAY)
    vm = _valid_bool_mask(valid_mask, mask.shape)
    mask[~vm] = 0

    n, labels, st, cent = cv2.connectedComponentsWithStats(mask, 8)
    final = np.zeros_like(mask, dtype=np.uint8)
    rows = []
    accepted = review = rejected = 0
    pa = pixel_area(image_data) if image_data is not None else None
    tol = max(1, int(edge_tolerance))
    edge_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*tol+1, 2*tol+1))

    for i in range(1, n):
        area = int(st[i, cv2.CC_STAT_AREA])
        if area <= 0:
            continue
        x, y, bw, bh = map(int, st[i, :4])
        comp = labels[y:y+bh, x:x+bw] == i
        vals = d[y:y+bh, x:x+bw][comp]
        if vals.size == 0:
            continue
        mean = float(vals.mean())
        p75 = float(np.percentile(vals, 75))
        p90 = float(np.percentile(vals, 90))
        vmax = float(vals.max())
        fill = area / max(float(bw*bh), 1.0)
        aspect = max(bw,bh) / max(min(bw,bh), 1)

        roi_u8 = comp.astype(np.uint8) * 255
        dist = cv2.distanceTransform((roi_u8 > 0).astype(np.uint8), cv2.DIST_L2, 5)
        depth = float(dist.max()) if dist.size else 0.0
        erode_r = max(1, min(3, tol//2 + 1))
        ek = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*erode_r+1, 2*erode_r+1))
        core = cv2.erode(roi_u8, ek) > 0
        core_frac = float(np.count_nonzero(core) / max(area,1))
        core_mean = float(d[y:y+bh, x:x+bw][core].mean()) if np.any(core) else 0.0

        contours, _ = cv2.findContours(roi_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        perimeter = sum(cv2.arcLength(c, True) for c in contours) if contours else 0.0
        compact = float(4.0*np.pi*area / max(perimeter*perimeter, 1e-6))

        # Edge coincidence test inside this component only, avoiding huge full-image
        # temporary arrays for 8k imagery.  A candidate dominated by edges present
        # in both aligned images is likely residual registration/illumination.
        pad = tol + 4
        x0=max(0,x-pad); y0=max(0,y-pad); x1=min(w,x+bw+pad); y1=min(h,y+bh+pad)
        r1 = g1[y0:y1, x0:x1]; r2 = g2[y0:y1, x0:x1]
        e1 = cv2.Canny(cv2.GaussianBlur(r1,(3,3),0), 55, 145)
        e2 = cv2.Canny(cv2.GaussianBlur(r2,(3,3),0), 55, 145)
        e1d = cv2.dilate(e1, edge_k); e2d = cv2.dilate(e2, edge_k)
        common = (e1d>0) & (e2d>0)
        cy0=y-y0; cx0=x-x0
        common_crop = common[cy0:cy0+bh, cx0:cx0+bw]
        edge_fraction = float(np.count_nonzero(common_crop & comp) / max(area,1))

        strong_cut = max(45.0, p75)
        strong_frac = float(np.count_nonzero(vals >= strong_cut) / max(vals.size,1))
        intensity_score = float(np.clip((p90/255.0 - 0.12) / 0.30, 0, 1))
        mean_score = float(np.clip((mean/255.0 - 0.09) / 0.24, 0, 1))
        core_score = float(np.clip(depth / max(tol+2.0, 4.0), 0, 1))
        fill_score = float(np.clip(fill / 0.34, 0, 1))
        interior_score = float(np.clip(core_frac / 0.28, 0, 1))
        strong_score = float(np.clip(strong_frac / 0.35, 0, 1))
        area_score = float(np.clip(np.log1p(area) / np.log1p(8000.0), 0, 1))
        core_evidence = float(np.clip((core_mean/255.0 - 0.08) / 0.25, 0, 1)) if core_mean else 0.0

        line_like = depth < max(2.2, 0.70*tol) and (fill < 0.18 or aspect > 7.0 or compact < 0.025)
        edge_dominated = edge_fraction > 0.68 and (core_frac < 0.18 or core_evidence < 0.35)

        conf = (0.22*intensity_score + 0.13*mean_score + 0.16*core_score +
                0.10*fill_score + 0.13*interior_score + 0.10*strong_score +
                0.08*area_score + 0.08*core_evidence - 0.22*edge_fraction)
        if line_like:
            conf -= 0.18
        if edge_dominated:
            conf -= 0.16
        conf = float(np.clip(conf, 0, 1))
        conf_pct = conf*100.0

        # Conservative decision: large, filled components get a small allowance;
        # thin/common-edge components never bypass the edge-dominated rule.
        accepted_here = (conf_pct >= float(accept_confidence) and not edge_dominated)
        if (not edge_dominated and area >= 2500 and depth >= max(3.0,0.8*tol) and
                conf_pct >= float(accept_confidence)-5):
            accepted_here = True

        if accepted_here:
            classification = 'تغير مرجح'
            accepted += 1
            full_comp = labels == i
            final[full_comp] = 255
        elif conf_pct >= float(review_confidence):
            classification = 'مراجعة'
            review += 1
        else:
            classification = 'حافة/إضاءة'
            rejected += 1

        # Keep accepted and review items in the table; rejected noise is summarized
        # numerically so the GUI remains responsive even with thousands of rejects.
        if classification != 'حافة/إضاءة':
            rows.append({
                'id': i, 'x': x, 'y': y, 'width': bw, 'height': bh,
                'area_pixels': area,
                'area_map_units2': float(area*pa) if pa is not None else None,
                'mean_intensity': mean/255.0, 'max_intensity': vmax/255.0,
                'cx': float(cent[i][0]), 'cy': float(cent[i][1]),
                'confidence': conf_pct, 'classification': classification,
                'edge_fraction': edge_fraction, 'depth_px': depth,
            })

    final[~vm] = 0
    rows.sort(key=lambda r: (r['classification'] != 'تغير مرجح', -r['confidence'], -r['area_pixels']))
    summary = {
        'candidate_count': int(n-1),
        'accepted_count': int(accepted),
        'review_count': int(review),
        'rejected_count': int(rejected),
        'accept_confidence': float(accept_confidence),
        'review_confidence': float(review_confidence),
    }
    return final, rows, summary



def _best_local_similarity(a, b, radius=6):
    """Brightness-tolerant local structural similarity with a small shift search."""
    a=np.asarray(a,np.float32); b=np.asarray(b,np.float32)
    if min(a.shape[:2]) < 12: return 0.0
    # remove slow illumination field
    aa=a-cv2.GaussianBlur(a,(0,0),3.0); bb=b-cv2.GaussianBlur(b,(0,0),3.0)
    scale=min(1.0,256.0/max(a.shape[:2]))
    if scale<.999:
        sz=(max(12,int(a.shape[1]*scale)),max(12,int(a.shape[0]*scale)))
        aa=cv2.resize(aa,sz,interpolation=cv2.INTER_AREA); bb=cv2.resize(bb,sz,interpolation=cv2.INTER_AREA)
        radius=max(1,int(round(radius*scale)))
    r=min(radius,max(1,min(aa.shape)//5))
    def one(t,s):
        if s.shape[0] <= 2*r+4 or s.shape[1] <= 2*r+4: return 0.0
        tpl=t[r:t.shape[0]-r,r:t.shape[1]-r]
        if tpl.size<64: return 0.0
        try:
            res=cv2.matchTemplate(s,tpl,cv2.TM_CCOEFF_NORMED)
            return float(np.nanmax(res)) if res.size else 0.0
        except Exception: return 0.0
    c=.5*(one(aa,bb)+one(bb,aa))
    return float(np.clip((c+1.0)/2.0,0,1))


def _bidirectional_presence(g1,g2,x0,y0,x1,y1,roi,search_radius=6):
    """Conservative before/after presence verification for one candidate group.
    Scores are relative structural evidence, not semantic class probabilities.
    """
    h,w=g1.shape[:2]
    bw=max(1,x1-x0); bh=max(1,y1-y0)
    pad=int(np.clip(max(12,0.22*max(bw,bh),2*search_radius),12,56))
    xa=max(0,x0-pad); ya=max(0,y0-pad); xb=min(w,x1+pad); yb=min(h,y1+pad)
    a=g1[ya:yb,xa:xb]; b=g2[ya:yb,xa:xb]
    if a.size==0 or b.size==0:
        return dict(before_presence_pct=50.0,after_presence_pct=50.0,cross_similarity_pct=0.0,edge_overlap_pct=0.0)
    au=np.clip(a,0,255).astype(np.uint8); bu=np.clip(b,0,255).astype(np.uint8)
    sim=_best_local_similarity(a,b,search_radius)
    e1=cv2.Canny(au,55,145); e2=cv2.Canny(bu,55,145)
    k=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5))
    d1=cv2.dilate(e1,k); d2=cv2.dilate(e2,k)
    n1=max(1,np.count_nonzero(e1)); n2=max(1,np.count_nonzero(e2))
    ov1=np.count_nonzero((e1>0)&(d2>0))/n1; ov2=np.count_nonzero((e2>0)&(d1>0))/n2
    edge_overlap=.5*(ov1+ov2)
    def energy(x):
        hp=x-cv2.GaussianBlur(x,(0,0),3.0)
        gx=cv2.Sobel(x,cv2.CV_32F,1,0,ksize=3); gy=cv2.Sobel(x,cv2.CV_32F,0,1,ksize=3)
        return float(np.mean(np.abs(hp))+.25*np.std(x)+.08*np.mean(cv2.magnitude(gx,gy)))
    ea,eb=energy(a),energy(b); mx=max(ea,eb,1e-6)
    pa=100.0*ea/mx; pb=100.0*eb/mx
    same=.68*sim+.32*edge_overlap
    return dict(before_presence_pct=float(np.clip(pa,0,100)),after_presence_pct=float(np.clip(pb,0,100)),
                cross_similarity_pct=float(np.clip(same*100,0,100)),edge_overlap_pct=float(np.clip(edge_overlap*100,0,100)))



def _shift_overlap_slices(h, w, dx, dy):
    """Return source/destination slices for comparing A(y,x) with shifted B."""
    if dx >= 0:
        ax0, ax1, bx0, bx1 = dx, w, 0, w-dx
    else:
        ax0, ax1, bx0, bx1 = 0, w+dx, -dx, w
    if dy >= 0:
        ay0, ay1, by0, by1 = dy, h, 0, h-dy
    else:
        ay0, ay1, by0, by1 = 0, h+dy, -dy, h
    return (slice(ay0, ay1), slice(ax0, ax1)), (slice(by0, by1), slice(bx0, bx1))


def _object_correspondence_verification(g1, g2, x0, y0, x1, y1, roi, search_radius=6):
    """Verify that the *same physical structure* is present in BEFORE and AFTER.

    This stage is deliberately appearance tolerant.  It evaluates a dilated object
    context around a fragmented change candidate using high-pass structure,
    gradient energy and edge overlap, while allowing a few pixels of translation.
    It is not a semantic detector; the score is evidence of object correspondence.
    """
    h, w = g1.shape[:2]
    bw, bh = max(1, x1-x0), max(1, y1-y0)
    obj_span = max(bw, bh)
    pad = int(np.clip(max(24, 0.70*obj_span, 3*search_radius), 24, 120))
    xa, ya = max(0, x0-pad), max(0, y0-pad)
    xb, yb = min(w, x1+pad), min(h, y1+pad)
    a = g1[ya:yb, xa:xb].astype(np.float32)
    b = g2[ya:yb, xa:xb].astype(np.float32)
    if min(a.shape[:2]) < 12 or b.shape != a.shape:
        return dict(object_correspondence_pct=0.0, object_edge_overlap_pct=0.0,
                    object_before_strength_pct=50.0, object_after_strength_pct=50.0,
                    object_shift_px=0.0, shadow_likelihood_pct=0.0)

    # Place the original candidate mask into the larger context and expand it so a
    # fragmented wing/nose/tail candidate can vote using the whole nearby object.
    focus = np.zeros(a.shape, np.uint8)
    oy0, ox0 = y0-ya, x0-xa
    rh, rw = roi.shape[:2]
    fy1, fx1 = min(focus.shape[0], oy0+rh), min(focus.shape[1], ox0+rw)
    if fy1 > oy0 and fx1 > ox0:
        focus[oy0:fy1, ox0:fx1] = (roi[:fy1-oy0, :fx1-ox0] > 0).astype(np.uint8)
    grow = int(np.clip(max(10, 0.45*obj_span), 10, 56))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*grow+1, 2*grow+1))
    focus = cv2.dilate(focus, k)
    # Do not let a very large dilation turn this into a background-comparison test.
    if np.count_nonzero(focus) > 0.72*focus.size:
        grow2 = max(6, grow//2)
        k2 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*grow2+1, 2*grow2+1))
        focus[:] = 0
        focus[oy0:fy1, ox0:fx1] = (roi[:fy1-oy0, :fx1-ox0] > 0).astype(np.uint8)
        focus = cv2.dilate(focus, k2)

    # Radiometric/illumination tolerance: compare local detail after removing the
    # slow brightness field.  This is much less sensitive to sun angle and shadows.
    sigma = float(np.clip(min(a.shape[:2])/18.0, 3.0, 9.0))
    ah = a - cv2.GaussianBlur(a, (0,0), sigma)
    bhp = b - cv2.GaussianBlur(b, (0,0), sigma)
    agx = cv2.Sobel(ah, cv2.CV_32F, 1,0,ksize=3); agy = cv2.Sobel(ah, cv2.CV_32F,0,1,ksize=3)
    bgx = cv2.Sobel(bhp,cv2.CV_32F,1,0,ksize=3); bgy = cv2.Sobel(bhp,cv2.CV_32F,0,1,ksize=3)
    amag = cv2.magnitude(agx, agy); bmag = cv2.magnitude(bgx, bgy)
    au = np.clip(a,0,255).astype(np.uint8); bu = np.clip(b,0,255).astype(np.uint8)
    e1 = cv2.Canny(au, 45, 135); e2 = cv2.Canny(bu, 45, 135)

    # Global alignment is already sub-pixel/few-pixel accurate, so a compact local
    # search is sufficient and avoids matching a neighbouring aircraft/building.
    r = int(np.clip(search_radius, 2, 8))
    best = (-1.0, 0.0, 0.0, 0, 0)  # score, edge, ncc, dx, dy
    for dy in range(-r, r+1):
        for dx in range(-r, r+1):
            sa, sb = _shift_overlap_slices(a.shape[0], a.shape[1], dx, dy)
            m = focus[sa] > 0
            if np.count_nonzero(m) < 80:
                continue
            va = ah[sa][m]; vb = bhp[sb][m]
            va = va - va.mean(); vb = vb - vb.mean()
            den = float(np.sqrt(np.dot(va,va)*np.dot(vb,vb)) + 1e-6)
            ncc = float(np.dot(va,vb)/den)
            ncc01 = float(np.clip((ncc+1.0)/2.0, 0, 1))

            # Edge overlap with a small tolerance.  This retains geometric shape
            # even if the object is brighter/darker or its shadow has moved.
            ea = e1[sa]; eb = e2[sb]
            kk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(5,5))
            deb = cv2.dilate(eb,kk); dea = cv2.dilate(ea,kk)
            em = m
            na = max(1, np.count_nonzero((ea>0)&em)); nb = max(1, np.count_nonzero((eb>0)&em))
            ova = np.count_nonzero((ea>0)&(deb>0)&em)/na
            ovb = np.count_nonzero((eb>0)&(dea>0)&em)/nb
            eov = 0.5*(ova+ovb)

            ga = amag[sa][m]; gb = bmag[sb][m]
            ga = ga-ga.mean(); gb = gb-gb.mean()
            gden = float(np.sqrt(np.dot(ga,ga)*np.dot(gb,gb))+1e-6)
            gcorr = float(np.clip((np.dot(ga,gb)/gden+1.0)/2.0,0,1))
            score = .50*ncc01 + .35*eov + .15*gcorr
            if score > best[0]: best=(score,eov,ncc01,dx,dy)

    score,eov,ncc01,bdx,bdy = best
    if score < 0: score=eov=ncc01=0.0

    # Presence strength is measured only in the object-context mask, not over the
    # whole crop.  This helps distinguish a disappeared object from a static apron.
    fm = focus > 0
    def strength(hp, mag, edge):
        vals=np.abs(hp[fm]); g=mag[fm]; ed=edge[fm]
        if vals.size == 0: return 0.0
        return float(np.mean(vals) + .22*np.mean(g) + 16.0*np.mean(ed>0))
    sa0, sb0 = strength(ah,amag,e1), strength(bhp,bmag,e2)
    mx=max(sa0,sb0,1e-6)
    pa,pb=100.0*sa0/mx,100.0*sb0/mx

    # High structural correspondence plus a sizeable mean luminance change is a
    # classic shadow/illumination signature.  It is informative, not a hard delete.
    ma=float(np.mean(a[fm])) if np.any(fm) else 0.0
    mb=float(np.mean(b[fm])) if np.any(fm) else 0.0
    lum_delta=min(1.0,abs(ma-mb)/55.0)
    shadow=float(np.clip(100.0*(0.65*eov+0.35*score)*lum_delta,0,100))
    return dict(object_correspondence_pct=float(np.clip(score*100,0,100)),
                object_edge_overlap_pct=float(np.clip(eov*100,0,100)),
                object_ncc_pct=float(np.clip(ncc01*100,0,100)),
                object_before_strength_pct=float(np.clip(pa,0,100)),
                object_after_strength_pct=float(np.clip(pb,0,100)),
                object_shift_px=float((bdx*bdx+bdy*bdy)**0.5),
                shadow_likelihood_pct=shadow)

def verified_mask_from_objects(shape, objects):
    """Rasterize only verified real changes (appearance/disappearance/structural)."""
    h,w=shape[:2]; out=np.zeros((h,w),np.uint8)
    for o in objects:
        if not o.get('is_real_change',False): continue
        for poly in o.get('polygons',[]) or []:
            pts=np.asarray(poly,dtype=np.int32).reshape(-1,1,2)
            if len(pts)>=3: cv2.fillPoly(out,[pts],255)
    return out

def group_change_regions(final_mask, regions, reference=None, moving=None, image_data=None,
                         valid_mask=None, merge_distance=12):
    """Group nearby accepted change components into higher-level change groups.

    This does NOT claim semantic object identity (car/building/etc.).  It groups
    accepted Post-Filter components that are spatially close so fragmented parts
    of one physical change are counted once.  The original accepted mask is not
    enlarged; dilation is used only to discover group membership.

    Returns: annotated_regions, objects, summary
      annotated_regions: input rows with ``object_id`` added for accepted rows
      objects: one row per spatial change group
      summary: counts and merge distance
    """
    if final_mask.ndim == 3:
        final_mask = cv2.cvtColor(final_mask, cv2.COLOR_RGB2GRAY)
    base = (final_mask > 0).astype(np.uint8)
    if valid_mask is not None:
        vm = _valid_bool_mask(valid_mask, base.shape)
        base[~vm] = 0
    else:
        vm = np.ones(base.shape, dtype=bool)

    md = max(0, int(merge_distance))
    if not np.any(base):
        rows=[dict(r, object_id=None) for r in regions]
        return rows, [], {'object_count':0, 'accepted_region_count':0, 'merge_distance_px':md}

    # Dilation is only a graph-building device.  It never changes final_mask.
    if md > 0:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*md+1, 2*md+1))
        link = cv2.dilate(base, k)
    else:
        link = base.copy()
    ng, glabel, _, _ = cv2.connectedComponentsWithStats(link, 8)

    # Original accepted connected components and their mapping to the dilated group.
    nc, clabel, cstats, ccent = cv2.connectedComponentsWithStats(base, 8)
    comp_to_group = {}
    for ci in range(1, nc):
        yy, xx = np.where(clabel == ci)
        if yy.size == 0:
            continue
        gids = glabel[yy, xx]
        gids = gids[gids > 0]
        if gids.size:
            vals, counts = np.unique(gids, return_counts=True)
            comp_to_group[ci] = int(vals[np.argmax(counts)])

    # Map post-filter region rows to current connected components using centroid,
    # with a bbox-center fallback for numerical/rounding edge cases.
    rows=[]
    group_conf={}
    group_parts={}
    group_class={}
    for r in regions:
        rr=dict(r)
        if rr.get('classification','تغير مرجح') != 'تغير مرجح':
            rr['object_id']=None
            rows.append(rr)
            continue
        cx=int(round(float(rr.get('cx', rr.get('x',0)+rr.get('width',1)/2))))
        cy=int(round(float(rr.get('cy', rr.get('y',0)+rr.get('height',1)/2))))
        ci=0
        if 0 <= cy < clabel.shape[0] and 0 <= cx < clabel.shape[1]:
            ci=int(clabel[cy,cx])
        if ci <= 0:
            x=int(rr.get('x',0)); y=int(rr.get('y',0)); bw=int(rr.get('width',1)); bh=int(rr.get('height',1))
            roi=clabel[max(0,y):min(clabel.shape[0],y+bh), max(0,x):min(clabel.shape[1],x+bw)]
            ids=roi[roi>0]
            if ids.size:
                vals,counts=np.unique(ids,return_counts=True); ci=int(vals[np.argmax(counts)])
        gid=comp_to_group.get(ci)
        rr['_group_label']=gid
        rows.append(rr)

    # Re-number group labels densely from 1..N using accepted rows only.
    used=sorted({r.get('_group_label') for r in rows if r.get('_group_label')})
    dense={g:i+1 for i,g in enumerate(used)}
    for rr in rows:
        g=rr.pop('_group_label',None)
        rr['object_id']=dense.get(g) if g else None
        if rr['object_id'] is not None:
            oid=rr['object_id']
            group_parts.setdefault(oid,[]).append(rr)

    pa = pixel_area(image_data) if image_data is not None else None
    g1=g2=None
    if reference is not None and moving is not None:
        ref = reference if reference.shape[:2]==base.shape else cv2.resize(reference,(base.shape[1],base.shape[0]),interpolation=cv2.INTER_LINEAR)
        mov = moving if moving.shape[:2]==base.shape else cv2.resize(moving,(base.shape[1],base.shape[0]),interpolation=cv2.INTER_LINEAR)
        g1=cv2.cvtColor(ref,cv2.COLOR_RGB2GRAY).astype(np.float32)
        g2=cv2.cvtColor(mov,cv2.COLOR_RGB2GRAY).astype(np.float32)

    objects=[]
    for oid, parts in sorted(group_parts.items()):
        # Build the exact original-pixel mask for this group from member bboxes.
        xs=[int(r['x']) for r in parts]; ys=[int(r['y']) for r in parts]
        x2=[int(r['x'])+int(r['width']) for r in parts]; y2=[int(r['y'])+int(r['height']) for r in parts]
        x0,y0,x1,y1=min(xs),min(ys),max(x2),max(y2)
        roi=np.zeros((y1-y0,x1-x0),np.uint8)
        for r in parts:
            x=int(r['x']); y=int(r['y']); bw=int(r['width']); bh=int(r['height'])
            roi[(y-y0):(y-y0+bh),(x-x0):(x-x0+bw)] |= base[y:y+bh,x:x+bw]
        area=int(np.count_nonzero(roi))
        confs=np.array([float(r.get('confidence',0)) for r in parts],dtype=float)
        areas=np.array([max(1,int(r.get('area_pixels',1))) for r in parts],dtype=float)
        conf=float(np.average(confs,weights=areas)) if confs.size else 0.0
        signed_mean=0.0
        direction='تغير مكاني'
        change_type='مراجعة / غير مؤكد'
        type_confidence=float(np.clip(conf,0,100))
        edge_before=edge_after=texture_before=texture_after=0.0
        edge_ratio=1.0
        before_presence=after_presence=cross_similarity=edge_overlap=0.0
        object_correspondence=object_edge_overlap=object_before_strength=object_after_strength=0.0
        object_shift=shadow_likelihood=0.0
        is_real_change=False
        verification_note='لم يتم التحقق'
        if g1 is not None and np.any(roi):
            rr1=g1[y0:y1,x0:x1]; rr2=g2[y0:y1,x0:x1]; sel=roi>0
            delta=(rr2-rr1)[sel]; signed_mean=float(delta.mean()) if delta.size else 0.0
            if signed_mean >= 8.0: direction='أفتح/زيادة بعد'
            elif signed_mean <= -8.0: direction='أغمق/نقصان بعد'
            else: direction='تغير مختلط'
            gx1=cv2.Sobel(rr1,cv2.CV_32F,1,0,ksize=3); gy1=cv2.Sobel(rr1,cv2.CV_32F,0,1,ksize=3)
            gx2=cv2.Sobel(rr2,cv2.CV_32F,1,0,ksize=3); gy2=cv2.Sobel(rr2,cv2.CV_32F,0,1,ksize=3)
            mag1=cv2.magnitude(gx1,gy1); mag2=cv2.magnitude(gx2,gy2)
            edge_before=float(mag1[sel].mean()) if np.any(sel) else 0.0; edge_after=float(mag2[sel].mean()) if np.any(sel) else 0.0
            texture_before=float(rr1[sel].std()) if np.any(sel) else 0.0; texture_after=float(rr2[sel].std()) if np.any(sel) else 0.0
            edge_ratio=(edge_after+4.0)/(edge_before+4.0)
            ver=_bidirectional_presence(g1,g2,x0,y0,x1,y1,roi,search_radius=max(4,int(md/2)))
            before_presence=ver['before_presence_pct']; after_presence=ver['after_presence_pct']; cross_similarity=ver['cross_similarity_pct']; edge_overlap=ver['edge_overlap_pct']
            corr=_object_correspondence_verification(g1,g2,x0,y0,x1,y1,roi,search_radius=max(4,int(md/2)))
            object_correspondence=corr['object_correspondence_pct']; object_edge_overlap=corr['object_edge_overlap_pct']
            object_before_strength=corr['object_before_strength_pct']; object_after_strength=corr['object_after_strength_pct']
            object_shift=corr['object_shift_px']; shadow_likelihood=corr['shadow_likelihood_pct']

            # Object-first decision.  A fragmented change component is not allowed to
            # become an appearance/disappearance if the surrounding object geometry
            # still corresponds in both images.  This directly targets aircraft/building
            # cases where only lighting, shadow or a few edge pixels changed.
            old_stable=((cross_similarity >= 62.0 and min(before_presence,after_presence) >= 66.0 and abs(before_presence-after_presence) <= 24.0) or
                        (min(before_presence,after_presence)>=82.0 and abs(before_presence-after_presence)<=14.0 and cross_similarity>=46.0))
            object_stable=((object_correspondence >= 68.0 and object_edge_overlap >= 52.0 and min(object_before_strength,object_after_strength) >= 55.0) or
                           (object_correspondence >= 61.0 and conf >= 70.0 and object_edge_overlap >= 44.0 and min(object_before_strength,object_after_strength) >= 62.0))
            if old_stable or object_stable:
                change_type='موجود في الصورتين (لا تغير حقيقي)'; is_real_change=False
                type_confidence=float(np.clip(.46*object_correspondence+.24*object_edge_overlap+.18*cross_similarity+.12*min(object_before_strength,object_after_strength),0,100))
                if shadow_likelihood >= 42.0:
                    verification_note='مطابقة جسم مؤكدة في الصورتين؛ اختلاف الظل/الإضاءة مستبعد من التغيير الحقيقي'
                else:
                    verification_note='تمت مطابقة الجسم نفسه بنيويًا في BEFORE وAFTER'
            elif (before_presence>=84.0 and object_before_strength>=84.0 and after_presence<=64.0 and object_after_strength<=64.0 and
                  max(before_presence-after_presence,object_before_strength-object_after_strength)>=28.0 and object_correspondence<62.0 and object_edge_overlap<66.0):
                change_type='اختفاء حقيقي'; is_real_change=True
                evidence=max(before_presence-after_presence,object_before_strength-object_after_strength)
                type_confidence=float(np.clip(24.0+.50*evidence+.18*(100-object_correspondence)+.08*(100-object_edge_overlap),0,100))
                verification_note='الجسم مدعوم في BEFORE ولا توجد مطابقة جسم كافية في AFTER'
            elif (after_presence>=84.0 and object_after_strength>=84.0 and before_presence<=64.0 and object_before_strength<=64.0 and
                  max(after_presence-before_presence,object_after_strength-object_before_strength)>=28.0 and object_correspondence<62.0 and object_edge_overlap<66.0):
                change_type='ظهور حقيقي'; is_real_change=True
                evidence=max(after_presence-before_presence,object_after_strength-object_before_strength)
                type_confidence=float(np.clip(24.0+.50*evidence+.18*(100-object_correspondence)+.08*(100-object_edge_overlap),0,100))
                verification_note='الجسم مدعوم في AFTER ولا توجد مطابقة جسم كافية في BEFORE'
            elif min(object_before_strength,object_after_strength)>=60.0 and object_correspondence < 47.0 and cross_similarity < 52.0:
                change_type='تغير بنيوي/موضعي'; is_real_change=True
                type_confidence=float(np.clip(.45*(100-object_correspondence)+.25*(100-cross_similarity)+.30*min(object_before_strength,object_after_strength),0,100))
                verification_note='الجسم موجود في الصورتين لكن بنيته/موضعه لا يتطابقان بما يكفي'
            else:
                change_type='مراجعة / غير مؤكد'; is_real_change=False
                type_confidence=float(np.clip(.30*conf+.35*object_correspondence+.20*max(object_before_strength,object_after_strength)+.15*cross_similarity,0,100))
                verification_note='مطابقة الجسم غير حاسمة؛ لم يتم فرض ظهور/اختفاء لتجنب نتيجة كاذبة'
        # Exact polygon outlines for this spatial group.  These contours are
        # derived from the final accepted mask, not from the bounding rectangle.
        # They are stored as plain Python lists so project JSON export stays portable.
        polygons=[]
        contours,_=cv2.findContours((roi>0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            if cv2.contourArea(cnt) < 4.0:
                continue
            peri=cv2.arcLength(cnt,True)
            eps=max(0.75, 0.006*peri)
            approx=cv2.approxPolyDP(cnt,eps,True).reshape(-1,2)
            if len(approx) < 3:
                continue
            polygons.append([[int(px+x0),int(py+y0)] for px,py in approx])
        polygons.sort(key=lambda poly: abs(cv2.contourArea(np.asarray(poly,dtype=np.float32))), reverse=True)

        objects.append({
            'object_id':oid, 'parts':len(parts), 'x':x0, 'y':y0, 'width':x1-x0, 'height':y1-y0,
            'area_pixels':area, 'area_map_units2':float(area*pa) if pa is not None else None,
            'confidence':conf, 'direction':direction, 'signed_mean':signed_mean,
            'change_type':change_type, 'type_confidence':type_confidence,
            'edge_before':edge_before, 'edge_after':edge_after, 'edge_ratio':edge_ratio,
            'texture_before':texture_before, 'texture_after':texture_after,
            'before_presence_pct':before_presence, 'after_presence_pct':after_presence,
            'cross_similarity_pct':cross_similarity, 'edge_overlap_pct':edge_overlap,
            'object_correspondence_pct':object_correspondence, 'object_edge_overlap_pct':object_edge_overlap,
            'object_before_strength_pct':object_before_strength, 'object_after_strength_pct':object_after_strength,
            'object_shift_px':object_shift, 'shadow_likelihood_pct':shadow_likelihood,
            'is_real_change':bool(is_real_change), 'verification_note':verification_note,
            'cx':float((x0+x1)/2), 'cy':float((y0+y1)/2),
            'polygons':polygons,
        })
    # SECOND-PASS OBJECT PROTECTION
    # Strongly matched objects define a conservative protection zone.  Nearby
    # fragments caused by moved shadows, edge halos, or local parallax are not
    # allowed to become independent appearance/disappearance events unless they
    # carry very strong asymmetric presence evidence of their own.
    protection=np.zeros(base.shape,np.uint8)
    stable_seeds=[]
    for o in objects:
        if o.get('change_type')!='موجود في الصورتين (لا تغير حقيقي)':
            continue
        corr=float(o.get('object_correspondence_pct',0)); eov=float(o.get('object_edge_overlap_pct',0))
        xs=float(o.get('cross_similarity_pct',0)); st=min(float(o.get('object_before_strength_pct',0)),float(o.get('object_after_strength_pct',0)))
        if (corr>=61.0 and eov>=42.0 and st>=56.0) or (corr>=68.0 and xs>=48.0):
            stable_seeds.append(o)
            local=np.zeros(base.shape,np.uint8)
            for poly in o.get('polygons',[]) or []:
                pts=np.asarray(poly,np.int32).reshape(-1,1,2)
                if len(pts)>=3: cv2.fillPoly(local,[pts],255)
            if not np.any(local):
                x=int(o.get('x',0)); y=int(o.get('y',0)); bw=int(o.get('width',1)); bh=int(o.get('height',1))
                cv2.rectangle(local,(x,y),(min(base.shape[1]-1,x+bw),min(base.shape[0]-1,y+bh)),255,-1)
            span=max(int(o.get('width',1)),int(o.get('height',1)))
            rad=int(np.clip(8+0.14*span,10,48))
            kk=cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*rad+1,2*rad+1))
            local=cv2.dilate(local,kk)
            protection=cv2.bitwise_or(protection,local)

    protected_fragment_count=0
    if np.any(protection):
        for o in objects:
            if o in stable_seeds or o.get('change_type')=='موجود في الصورتين (لا تغير حقيقي)':
                continue
            om=np.zeros(base.shape,np.uint8)
            for poly in o.get('polygons',[]) or []:
                pts=np.asarray(poly,np.int32).reshape(-1,1,2)
                if len(pts)>=3: cv2.fillPoly(om,[pts],255)
            if not np.any(om):
                x=int(o.get('x',0)); y=int(o.get('y',0)); bw=int(o.get('width',1)); bh=int(o.get('height',1))
                cv2.rectangle(om,(x,y),(min(base.shape[1]-1,x+bw),min(base.shape[0]-1,y+bh)),255,-1)
            pix=max(1,int(np.count_nonzero(om)))
            ov=float(np.count_nonzero((om>0)&(protection>0)))/pix
            cx=int(round(float(o.get('cx',0)))); cy=int(round(float(o.get('cy',0))))
            center_protected=(0<=cx<base.shape[1] and 0<=cy<base.shape[0] and protection[cy,cx]>0)
            bp=float(o.get('before_presence_pct',0)); ap=float(o.get('after_presence_pct',0))
            bs=float(o.get('object_before_strength_pct',0)); a_s=float(o.get('object_after_strength_pct',0))
            asym=max(abs(bp-ap),abs(bs-a_s))
            corr=float(o.get('object_correspondence_pct',0)); sh=float(o.get('shadow_likelihood_pct',0))
            # Preserve a truly new/removed object if the one-sided evidence is very
            # strong and the fragment is not mostly contained by a matched object.
            strong_independent=(o.get('change_type') in ('ظهور حقيقي','اختفاء حقيقي') and asym>=42.0 and corr<42.0 and ov<0.74)
            protect_hit=(ov>=0.56) or (center_protected and ov>=0.30) or (sh>=38.0 and ov>=0.34)
            if protect_hit and not strong_independent:
                o['change_type']='ظل/حافة تابعة لجسم مطابق (مستبعد)'
                o['is_real_change']=False
                o['protection_overlap_pct']=float(np.clip(ov*100,0,100))
                o['type_confidence']=float(np.clip(max(float(o.get('type_confidence',0)), 62.0+30.0*ov),0,99))
                o['verification_note']='تم استبعاد الجزء لأنه داخل منطقة حماية جسم مطابق في BEFORE وAFTER؛ مرجح ظل/حافة/Parallax تابع للجسم'
                protected_fragment_count+=1

    objects.sort(key=lambda o:o['area_pixels'], reverse=True)
    # Preserve stable dense IDs after size sort for display.
    remap={o['object_id']:i+1 for i,o in enumerate(objects)}
    for o in objects: o['object_id']=remap[o['object_id']]
    for r in rows:
        if r.get('object_id') in remap: r['object_id']=remap[r['object_id']]
    type_counts={
        'appearance_count':sum(1 for o in objects if o.get('change_type')=='ظهور حقيقي'),
        'disappearance_count':sum(1 for o in objects if o.get('change_type')=='اختفاء حقيقي'),
        'structural_count':sum(1 for o in objects if o.get('change_type')=='تغير بنيوي/موضعي'),
        'stable_count':sum(1 for o in objects if o.get('change_type')=='موجود في الصورتين (لا تغير حقيقي)'),
        'protected_fragment_count':sum(1 for o in objects if o.get('change_type')=='ظل/حافة تابعة لجسم مطابق (مستبعد)'),
        'verification_review_count':sum(1 for o in objects if o.get('change_type')=='مراجعة / غير مؤكد'),
        'verified_change_count':sum(1 for o in objects if o.get('is_real_change',False)),
    }
    summary={
        'object_count':len(objects),
        'accepted_region_count':sum(1 for r in rows if r.get('classification')=='تغير مرجح'),
        'merge_distance_px':md,
        **type_counts,
    }
    return rows, objects, summary

def absolute_difference(a, b):
    return cv2.absdiff(a, resize_like(a, b))


def grayscale_difference(a, b):
    b = resize_like(a, b)
    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
    return cv2.absdiff(g1, g2)


def ssim_difference(a, b):
    b = resize_like(a, b)
    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
    score, sim = structural_similarity(g1, g2, full=True, data_range=255)
    diff = ((1.0 - sim) * 255.0).clip(0, 255).astype(np.uint8)
    return float(score), diff


def edge_difference(a, b):
    b = resize_like(a, b)
    g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
    e1 = cv2.Canny(g1, 60, 160)
    e2 = cv2.Canny(g2, 60, 160)
    return cv2.absdiff(e1, e2)


def normalized_difference(a, b):
    b = resize_like(a, b).astype(np.float32)
    a = a.astype(np.float32)
    num = np.abs(a - b).mean(axis=2)
    den = (a + b).mean(axis=2) + 1.0
    nd = np.clip(num / den, 0, 1)
    return (nd * 255).astype(np.uint8)


def compare(a, b, method='SSIM', valid_mask=None, edge_tolerance=3):
    if method.upper() == 'STRUCTURAL':
        from structural_comparison import compare_features
        return compare_features(a, b, valid_mask)
    m = method.upper()
    vm = None
    if valid_mask is not None:
        vm = resize_like(a, valid_mask) if valid_mask.shape[:2] != a.shape[:2] else valid_mask
        if vm.ndim == 3:
            vm = vm[:, :, 0]
        vm = vm > 0
    if m == 'SSIM':
        b2 = resize_like(a, b)
        g1 = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
        g2 = cv2.cvtColor(b2, cv2.COLOR_RGB2GRAY)
        _, sim = structural_similarity(g1, g2, full=True, data_range=255)
        diff = ((1.0 - sim) * 255.0).clip(0,255).astype(np.uint8)
        if vm is not None:
            score = float(sim[vm].mean()) if np.any(vm) else 0.0
            diff[~vm] = 0
        else:
            score = float(sim.mean())
        return diff, {'similarity_percent': score * 100.0}
    if m in ('ABS', 'ABSOLUTE'):
        d = absolute_difference(a, b)
        gray = cv2.cvtColor(d, cv2.COLOR_RGB2GRAY)
    elif m == 'EDGE':
        gray = edge_difference(a, b)
    elif m in ('NORMALIZED', 'ND'):
        gray = normalized_difference(a, b)
    elif m in ('ROBUST FINAL','FINAL POSTFILTER','ROBUST V6', 'ROBUST6', 'ROBUST-V6'):
        gray, matched, edge_supp = robust_change_difference_v6(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    elif m in ('ROBUST V5', 'ROBUST5', 'ROBUST-V5'):
        gray, matched, edge_supp = robust_change_difference_v5(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    elif m in ('ROBUST V4', 'ROBUST4', 'ROBUST-V4'):
        gray, matched, edge_supp = robust_change_difference_v4(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    elif m in ('ROBUST V3', 'ROBUST3', 'ROBUST-V3'):
        gray, matched, edge_supp = robust_change_difference_v3(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    elif m in ('ROBUST V2', 'ROBUST2', 'ROBUST-V2'):
        gray, matched, edge_supp = robust_change_difference_v2(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    elif m in ('ROBUST', 'ROBUST CHANGE', 'ADVANCED'):
        gray, matched, edge_supp = robust_change_difference(a, b, valid_mask=valid_mask, edge_tolerance=edge_tolerance)
    else:
        raise ValueError('طريقة مقارنة غير معروفة.')
    if vm is not None:
        vals = gray[vm]
        gray = gray.copy(); gray[~vm] = 0
    else:
        vals = gray.ravel()
    if m == 'EDGE':
        similarity = 100.0 * (1.0 - float(np.count_nonzero(vals)) / max(vals.size,1))
    else:
        similarity = 100.0 * (1.0 - float(vals.mean()) / 255.0) if vals.size else 0.0
    meta = {'similarity_percent': similarity}
    if m in ('ROBUST FINAL','FINAL POSTFILTER','ROBUST V6','ROBUST6','ROBUST-V6','ROBUST V5','ROBUST5','ROBUST-V5','ROBUST V4','ROBUST4','ROBUST-V4','ROBUST V3','ROBUST3','ROBUST-V3','ROBUST V2', 'ROBUST2', 'ROBUST-V2', 'ROBUST', 'ROBUST CHANGE', 'ADVANCED'):
        if m in ('ROBUST FINAL','FINAL POSTFILTER','ROBUST V6','ROBUST6','ROBUST-V6'):
            rv = 6
        elif m in ('ROBUST V5','ROBUST5','ROBUST-V5'):
            rv = 5
        elif m in ('ROBUST V4','ROBUST4','ROBUST-V4'):
            rv = 4
        elif m in ('ROBUST V3','ROBUST3','ROBUST-V3'):
            rv = 3
        elif m in ('ROBUST V2','ROBUST2','ROBUST-V2'):
            rv = 2
        else:
            rv = 1
        meta.update({'radiometric_normalization': True, 'edge_tolerance_px': int(edge_tolerance), 'edge_misregistration_suppression': True, 'robust_version': rv, 'structural_correlation': rv >= 3, 'component_confidence_filter': rv >= 3, 'object_core_filter': rv >= 4, 'adaptive_noise_floor': rv >= 4, 'hysteresis_core_filter': rv >= 5, 'compactness_filter': rv >= 5, 'region_first_filter': rv >= 6, 'coarse_appearance_support': rv >= 6})
    return gray, meta

def change_mask(diff_gray, threshold=35, min_area=80, kernel=3, valid_mask=None):
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    threshold = int(np.clip(threshold, 0, 255))
    kernel = max(1, int(kernel))
    if kernel % 2 == 0:
        kernel += 1
    _, mask = cv2.threshold(diff_gray, threshold, 255, cv2.THRESH_BINARY)
    if valid_mask is not None:
        vm = valid_mask[:, :, 0] if getattr(valid_mask, 'ndim', 2) == 3 else valid_mask
        if vm.shape != mask.shape:
            vm = cv2.resize(vm, (mask.shape[1], mask.shape[0]), interpolation=cv2.INTER_NEAREST)
        mask[vm == 0] = 0
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    n, labels, st, _ = cv2.connectedComponentsWithStats(mask, 8)
    clean = np.zeros_like(mask)
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] >= int(min_area):
            clean[labels == i] = 255
    if valid_mask is not None:
        clean[vm == 0] = 0
    return clean


def overlay_changes(base, mask, alpha=0.48):
    out = base.copy()
    red = np.zeros_like(out)
    red[:, :, 0] = 255
    sel = mask > 0
    out[sel] = ((1.0 - alpha) * out[sel] + alpha * red[sel]).astype(np.uint8)
    return out


def blend_images(a, b, alpha=0.5):
    b = resize_like(a, b)
    return cv2.addWeighted(a, 1.0 - alpha, b, alpha, 0)


def heatmap(diff_gray):
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    cm = cv2.applyColorMap(diff_gray, cv2.COLORMAP_INFERNO)
    return cv2.cvtColor(cm, cv2.COLOR_BGR2RGB)


def pixel_area(image_data: ImageData):
    if image_data is None or image_data.transform is None:
        return None
    try:
        return abs(float(image_data.transform.a * image_data.transform.e))
    except Exception:
        return None


def region_table(mask, diff=None, image_data=None):
    """
    Full-resolution region analysis without tiling or downsampling.

    Performance improvements:
    - Reuse uint8 mask directly (no full-size duplicate).
    - Use OpenCV GRANA/BBDT connected components when available.
    - Compute intensity statistics inside each component bounding box only,
      instead of scanning the entire raster once for every region.
    """
    if mask is None:
        return []

    if mask.dtype == np.uint8:
        binary = mask
    else:
        binary = np.where(mask > 0, 255, 0).astype(np.uint8)

    try:
        if hasattr(cv2, "connectedComponentsWithStatsWithAlgorithm") and hasattr(cv2, "CCL_GRANA"):
            n, labels, st, cent = cv2.connectedComponentsWithStatsWithAlgorithm(
                binary, 8, cv2.CV_32S, cv2.CCL_GRANA
            )
        else:
            n, labels, st, cent = cv2.connectedComponentsWithStats(binary, 8)
    except Exception:
        n, labels, st, cent = cv2.connectedComponentsWithStats(binary, 8)

    pa = pixel_area(image_data)
    rows = []

    diff_arr = None
    if diff is not None:
        diff_arr = diff
        if getattr(diff_arr, "ndim", 2) == 3:
            diff_arr = cv2.cvtColor(diff_arr, cv2.COLOR_RGB2GRAY)

    for i in range(1, n):
        area_px = int(st[i, cv2.CC_STAT_AREA])
        x = int(st[i, cv2.CC_STAT_LEFT])
        y = int(st[i, cv2.CC_STAT_TOP])
        w = int(st[i, cv2.CC_STAT_WIDTH])
        h = int(st[i, cv2.CC_STAT_HEIGHT])

        mean_i = 0.0
        max_i = 0.0

        if diff_arr is not None and w > 0 and h > 0:
            lbl_roi = labels[y:y+h, x:x+w]
            diff_roi = diff_arr[y:y+h, x:x+w]
            sel = (lbl_roi == i)
            if np.any(sel):
                vals = diff_roi[sel]
                if vals.size:
                    mean_i = float(vals.mean() / 255.0)
                    max_i = float(vals.max() / 255.0)

        rows.append({
            "id": i,
            "x": x,
            "y": y,
            "width": w,
            "height": h,
            "area_pixels": area_px,
            "area_map_units2": float(area_px * pa) if pa is not None else None,
            "mean_intensity": mean_i,
            "max_intensity": max_i,
            "cx": float(cent[i][0]),
            "cy": float(cent[i][1]),
        })

    rows.sort(key=lambda r: r["area_pixels"], reverse=True)
    return rows


def stats(mask, image_data=None, diff=None, valid_mask=None):
    changed = int(np.count_nonzero(mask))
    if valid_mask is not None:
        vm = valid_mask[:, :, 0] if getattr(valid_mask, 'ndim', 2) == 3 else valid_mask
        total = int(np.count_nonzero(vm))
    else:
        total = int(mask.size)
    pct = (changed / total * 100.0) if total else 0.0
    result = {'changed_pixels': changed, 'total_pixels': total, 'changed_percent': pct}
    pa = pixel_area(image_data) if image_data is not None else None
    if pa is not None:
        result['changed_area_map_units2'] = changed * pa
    if diff is not None:
        vals = diff[mask > 0]
        result['mean_change_intensity'] = float(vals.mean() / 255.0) if vals.size else 0.0
        result['max_change_intensity'] = float(vals.max() / 255.0) if vals.size else 0.0
    return result


def image_metadata(image_data: ImageData):
    return {
        'path': image_data.path,
        'width': image_data.width,
        'height': image_data.height,
        'crs': str(image_data.crs) if image_data.crs else 'غير متوفر',
        'pixel_size': (
            f'{abs(image_data.transform.a):.6g} × {abs(image_data.transform.e):.6g}'
            if image_data.transform is not None else 'غير متوفر'
        )
    }


def save_geotiff(path, array, ref: ImageData):
    if rasterio is None or ref.profile is None:
        raise RuntimeError('الحفظ GeoTIFF يحتاج Rasterio وصورة مرجعية GeoTIFF.')
    profile = ref.profile.copy()
    if array.ndim == 2:
        profile.update(count=1, dtype=str(array.dtype), height=array.shape[0], width=array.shape[1], compress='deflate')
        with rasterio.open(path, 'w', **lossless_profile(profile)) as dst:
            dst.write(array, 1)
    else:
        arr = np.transpose(array, (2, 0, 1))
        profile.update(count=arr.shape[0], dtype=str(arr.dtype), height=array.shape[0], width=array.shape[1], compress='deflate')
        with rasterio.open(path, 'w', **lossless_profile(profile)) as dst:
            dst.write(arr)


def save_geojson(path, mask, ref: ImageData, diff=None):
    if rasterio is None or shapes is None or ref.transform is None:
        raise RuntimeError('التصدير GeoJSON يحتاج GeoTIFF جغرافيًا وRasterio.')
    feats = []
    for idx, (geom, val) in enumerate(shapes((mask > 0).astype(np.uint8), mask=(mask > 0), transform=ref.transform), start=1):
        if int(val) == 1:
            feats.append({'type': 'Feature', 'properties': {'id': idx}, 'geometry': geom})
    fc = {'type': 'FeatureCollection', 'features': feats}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(fc, f, ensure_ascii=False, indent=2)


def histogram(diff_gray, bins=32):
    if diff_gray.ndim == 3:
        diff_gray = cv2.cvtColor(diff_gray, cv2.COLOR_RGB2GRAY)
    hist, edges = np.histogram(diff_gray.ravel(), bins=bins, range=(0, 256))
    return hist.astype(float), edges.astype(float)
