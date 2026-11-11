"""Read-only, bounded-memory illumination and colour inspection.

Thresholds are practical heuristics, not sensor calibration standards. Statistics
use shared valid pixels and original DN values; no per-image contrast stretch is
used for radiometric measurements. Geometry previews alone are stretched.
"""
from __future__ import annotations

import cv2
import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling

THRESHOLDS = {'brightness_percent': 8.0, 'color_percent': 5.0,
              'histogram_percent': 10.0}
SIMILAR = 'الإضاءة والألوان متقاربة — لا تحتاج توحيد Radiometric'
DIFFERENT = 'يوجد اختلاف واضح في الإضاءة أو الألوان — يوصى بتشغيل Radiometric / Histogram Match'


def _sample(ds, bands, shape):
    data = ds.read(bands, out_shape=(len(bands), *shape), resampling=Resampling.nearest)
    masks = ds.read_masks(bands, out_shape=(len(bands), *shape), resampling=Resampling.nearest)
    valid = np.all(masks > 0, axis=0) & np.all(np.isfinite(data), axis=0)
    for i, band in enumerate(bands):
        nodata = ds.nodatavals[band - 1]
        if nodata is not None:
            valid &= data[i] != nodata
    return data, valid


def _nonblack(data):
    threshold = 2 if data.dtype == np.uint8 else 0
    return np.any(np.abs(data.astype(np.float64)) > threshold, axis=0)


def _geometry_check(a, b, valid, scale):
    """Only measure registration; never align or modify either image."""
    import engine
    ga = np.mean(a[:min(3, len(a))].astype(np.float32), axis=0)
    gb = np.mean(b[:min(3, len(b))].astype(np.float32), axis=0)
    values = np.concatenate((ga[valid], gb[valid]))
    lo, hi = np.percentile(values, (1, 99))
    if hi - lo < 1e-9:
        raise RuntimeError('لا توجد تفاصيل كافية للتحقق من المواءمة الهندسية. نفّذ مواءمة موثوقة أولًا.')
    rgb = []
    for gray in (ga, gb):
        gray = np.where(valid, gray, lo)
        image = np.clip((gray - lo) * (255. / (hi - lo)), 0, 255).astype(np.uint8)
        image[~valid] = 0
        rgb.append(np.repeat(image[:, :, None], 3, axis=2))
    mask = valid.astype(np.uint8) * 255
    phase = engine._phase_tile_metrics(*rgb, mask, grid=4, tile_size=384)
    try:
        best, _ = engine._estimate_initial_transform(*rgb, scale, .72, 3., lambda *_: None)
        error = engine._reprojection_error(np.eye(3), best['src'], best['dst'], best['mask']) / scale
    except RuntimeError as exc:
        raise RuntimeError('تعذر تأكيد المواءمة الهندسية؛ نفّذ المواءمة أولًا ثم أعد الفحص.') from exc
    p90 = phase['p90_mag'] / scale
    if (phase['count'] < 4 or not np.isfinite(p90) or p90 >= 1.5
            or error >= 1.0 or best['inlier_ratio'] < .5 or best['coverage'] < .18):
        raise RuntimeError('المواءمة الهندسية مطلوبة قبل فحص الإضاءة والألوان '
                           f'(Residual P90={p90:.2f}px، Error={error:.2f}px).')
    return {'source': 'measured', 'reprojection_error_px': error, 'residual_p90_px': p90}


def _statistics(a, b, valid, labels):
    n = int(np.count_nonzero(valid))
    if n < 1024:
        raise RuntimeError('البكسلات المشتركة الصالحة غير كافية للفحص (يلزم 1024 عينة على الأقل).')
    stats = []
    means_a, means_b = [], []
    for i, label in enumerate(labels):
        x = np.asarray(a[i][valid], dtype=np.float64)
        y = np.asarray(b[i][valid], dtype=np.float64)
        mx, my = float(x.mean()), float(y.mean())
        px, py = np.percentile(x, (2, 50, 98)), np.percentile(y, (2, 50, 98))
        brightness = 100. * abs(mx - my) / max(abs(mx), abs(my), 1e-12)
        sx, sy = float(x.std()), float(y.std())
        contrast = 100. * abs(sx - sy) / max(sx, sy, 1e-12)
        # Normalized 1-D Wasserstein approximation: mean absolute CDF difference
        # on 128 shared bins spanning the pooled P2..P98 range. Clip tails so a
        # few extreme pixels cannot dilute a global distribution difference.
        lo, hi = min(px[0], py[0]), max(px[2], py[2])
        if hi - lo < 1e-12:
            histogram = 0.
        else:
            hx = np.histogram(np.clip(x, lo, hi), bins=128, range=(lo, hi))[0] / n
            hy = np.histogram(np.clip(y, lo, hi), bins=128, range=(lo, hi))[0] / n
            histogram = 100. * float(np.mean(np.abs(np.cumsum(hx) - np.cumsum(hy))))
        stats.append({'band': label,
                      'image1': {'mean': mx, 'std': float(x.std()), 'p2': float(px[0]),
                                 'p50': float(px[1]), 'p98': float(px[2])},
                      'image2': {'mean': my, 'std': float(y.std()), 'p2': float(py[0]),
                                 'p50': float(py[1]), 'p98': float(py[2])},
                      'brightness_difference_percent': brightness,
                      'contrast_difference_percent': contrast,
                      'histogram_difference_percent': histogram})
        means_a.append(mx); means_b.append(my)
    # Relative channel balance separates a colour cast from uniform gain.
    ma, mb = np.abs(means_a), np.abs(means_b)
    color = 50. * float(np.abs(ma / max(ma.sum(), 1e-12) - mb / max(mb.sum(), 1e-12)).sum())
    brightness = float(np.mean([s['brightness_difference_percent'] for s in stats]))
    histogram = float(np.mean([s['histogram_difference_percent'] for s in stats]))
    recommend = (color >= THRESHOLDS['color_percent']
                 or any(s['brightness_difference_percent'] >= THRESHOLDS['brightness_percent']
                        or s['histogram_difference_percent'] >= THRESHOLDS['histogram_percent'] for s in stats))
    return {'bands': stats, 'brightness_difference_percent': brightness,
            'contrast_difference_percent': float(np.mean([s['contrast_difference_percent'] for s in stats])),
            'color_difference_percent': color, 'histogram_difference_percent': histogram,
            'recommend_match': bool(recommend), 'recommendation': DIFFERENT if recommend else SIMILAR,
            'thresholds': dict(THRESHOLDS), 'valid_samples': n,
            'valid_overlap_percent': 100. * n / valid.size}


def inspect_radiometry(reference, moving, *, alignment=None, max_side=1024):
    """Inspect all non-alpha bands on the same grid without writing any raster.

    Optional alignment is a snapshot of the application's approved result:
    report, H, aligned RGB and valid_mask. Native bands use its global transform;
    a mesh result uses the actual aligned RGB (only native uint8 RGB is supported
    for that case, since this application does not retain a native multiband mesh).
    """
    max_side = max(256, min(2048, int(max_side)))
    from preparation import inspect_raster, band_compatibility
    if band_compatibility(inspect_raster(reference),inspect_raster(moving))['mode']=='STRUCTURAL COMPARISON':
        raise RuntimeError('RGB ↔ NIR / Structural: فحص توزيع الشدة العادي غير صالح لهذا الزوج.')
    with rasterio.open(reference) as ref, rasterio.open(moving) as src:
        if (ref.crs is None or src.crs is None or ref.crs != src.crs
                or ref.shape != src.shape or not ref.transform.almost_equals(src.transform, precision=1e-12)):
            raise RuntimeError('يجب تطابق CRS / Pixel Size / Extent / Width / Height. وحّد الشبكة أولًا.')
        bands = [i for i, color in enumerate(ref.colorinterp, 1) if color != ColorInterp.alpha]
        other = [i for i, color in enumerate(src.colorinterp, 1) if color != ColorInterp.alpha]
        if (not bands or bands != other or ref.count != src.count
                or any(ref.dtypes[i-1] != src.dtypes[i-1]
                       or ref.colorinterp[i-1] != src.colorinterp[i-1] for i in bands)):
            raise RuntimeError('يجب تطابق عدد الحزم وأنواع بياناتها وترتيبها للفحص الإشعاعي.')
        scale = min(1., max_side / max(ref.shape))
        shape = (max(1, round(ref.height * scale)), max(1, round(ref.width * scale)))
        a, va = _sample(ref, bands, shape)
        b, vb = _sample(src, bands, shape)
        geometry = None
        if alignment is not None:
            report = alignment.get('report') or {}
            if not report.get('alignment_approved'):
                raise RuntimeError('المواءمة الحالية غير معتمدة. نفّذ مواءمة دقيقة أولًا.')
            if report.get('smooth_mesh_passes', 0):
                rgb = alignment.get('aligned')
                if (bands not in ([1], [1, 2, 3]) or a.dtype != np.uint8 or rgb is None
                        or rgb.shape != (ref.height, ref.width, 3)):
                    raise RuntimeError('فحص نتيجة Mesh متعددة الحزم يحتاج ملف GeoTIFF محاذى بالقيم الأصلية؛ '
                                       'لا يمكن استخدام RGB ممدد الإضاءة بدل الحزم الأصلية.')
                b = cv2.resize(rgb, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST_EXACT).transpose(2, 0, 1)
                if bands == [1]:
                    b = b[:1]
                vb = np.all(np.isfinite(b), axis=0)
            else:
                H = np.asarray(alignment.get('H'), dtype=np.float64)
                if H.shape != (3, 3) or not np.all(np.isfinite(H)) or abs(np.linalg.det(H)) < 1e-12:
                    raise RuntimeError('تحويل المواءمة غير صالح للفحص.')
                S = np.diag([shape[1] / ref.width, shape[0] / ref.height, 1.])
                Hs = S @ H @ np.linalg.inv(S)
                # Nearest sampling preserves native band distributions, with no
                # new radiometric interpolation or per-image normalization.
                b = np.stack([cv2.warpPerspective(band.astype(np.float64), Hs, (shape[1], shape[0]),
                                                  flags=cv2.INTER_NEAREST) for band in b])
                vb = cv2.warpPerspective(vb.astype(np.uint8), Hs, (shape[1], shape[0]),
                                         flags=cv2.INTER_NEAREST) > 0
            vm = alignment.get('valid_mask')
            if vm is None or vm.shape != ref.shape:
                raise RuntimeError('قناع التداخل للمواءمة غير متاح.')
            vb &= cv2.resize(vm, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST_EXACT) > 0
            geometry = {'source': 'approved_alignment', 'model': report.get('model'),
                        'smooth_mesh_passes': report.get('smooth_mesh_passes', 0)}
        # Treat black borders independently of declared NoData, on both images.
        valid = va & vb & _nonblack(a) & _nonblack(b.astype(a.dtype))
        if np.count_nonzero(valid) < 1024:
            raise RuntimeError('البكسلات المشتركة الصالحة غير كافية للفحص (يلزم 1024 عينة على الأقل).')
        if geometry is None:
            geometry = _geometry_check(a, b, valid, min(shape[0] / ref.height, shape[1] / ref.width))
        result = _statistics(a, b, valid, bands)
        result.update(geometry=geometry, sample_shape=list(shape),
                      reference=str(reference), moving=str(moving), radiometric_applied=False)
        return result


def format_report(result):
    lines = [result['recommendation'], '',
             f"Brightness difference %: {result['brightness_difference_percent']:.2f}",
             f"Contrast difference %: {result['contrast_difference_percent']:.2f}",
             f"Color difference %: {result['color_difference_percent']:.2f}",
             f"Histogram difference %: {result['histogram_difference_percent']:.2f}",
             f"Recommendation: {result['recommendation']}",
             f"Shared valid samples: {result['valid_samples']} ({result['valid_overlap_percent']:.2f}%)",
             f"Geometry: {result['geometry']['source']}", '',
             'Band | Image | Mean | Std | P2 | P50 | P98 (original DN)']
    for band in result['bands']:
        for key, name in (('image1', '1'), ('image2', '2')):
            s = band[key]
            lines.append(f"{band['band']} | {name} | " + ' | '.join(f'{s[k]:.3f}' for k in ('mean','std','p2','p50','p98')))
        lines.append(f"Band {band['band']}: Brightness={band['brightness_difference_percent']:.2f}% | "
                     f"Histogram={band['histogram_difference_percent']:.2f}%")
    lines.extend(['', 'الحدود الإرشادية: Brightness ≥ 8% أو Histogram ≥ 10% في أي Band، أو Color ≥ 5%.',
                  'Brightness: فرق المتوسط / أكبر متوسط مطلق. Color: فرق نسب متوسطات الحزم.',
                  'Histogram: متوسط فرق CDF على 128 خانة مشتركة بين P2 وP98.',
                  'الفحص بعينة موزعة؛ تغير محتوى المشهد قد يؤثر في التوصية. لم يتم تعديل الصور.'])
    return '\n'.join(lines)
