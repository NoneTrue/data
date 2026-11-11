"""Cross-spectral structure descriptors; never compare RGB and NIR radiance."""
import os
import tempfile
from pathlib import Path
import cv2
import numpy as np
import rasterio
from rasterio.windows import Window
from raster_profiles import lossless_profile

MODE = 'STRUCTURAL COMPARISON'


def descriptors(gray, valid):
    gray = np.asarray(gray, np.float32)
    valid = np.asarray(valid, bool)
    if valid.sum() < 32:
        return np.zeros((*gray.shape, 3), np.uint8), np.zeros_like(valid)
    # Local variance normalization removes gain, offset and contrast polarity.
    filled = gray.copy(); filled[~valid] = float(np.median(gray[valid]))
    channels = []
    for sigma in (1., 2., 4.):
        smooth = cv2.GaussianBlur(filled, (0, 0), sigma)
        gx = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
        mag = cv2.magnitude(gx, gy)
        energy = np.sqrt(cv2.GaussianBlur(mag * mag, (0, 0), 4.) + 1e-12)
        channels.append((3 + 252 * np.clip(mag / (2 * energy), 0, 1)).astype(np.uint8))
    safe = cv2.erode(valid.astype(np.uint8), np.ones((33, 33), np.uint8),
                     borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    result = np.stack(channels, axis=-1); result[~safe] = 0
    return result, safe


def prepare_pair(path_a, path_b, out_a, out_b, info_a, info_b, cancel=None, progress=None):
    from preparation import check_cancel, _read_analysis
    inputs = {os.path.normcase(os.path.realpath(p)) for p in (path_a, path_b)}
    if out_a == out_b or any(os.path.exists(p) or os.path.normcase(os.path.realpath(p)) in inputs for p in (out_a, out_b)):
        raise ValueError('Structural outputs must be new files, separate from originals.')
    staged = []
    try:
        for index, (path, output, info) in enumerate(((path_a, out_a, info_a), (path_b, out_b, info_b))):
            check_cancel(cancel)
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(suffix='.tif', dir=str(Path(output).parent)); os.close(fd)
            staged.append((tmp, output))
            with rasterio.open(path) as src, rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                profile = lossless_profile(src.profile, count=3, dtype='uint8', nodata=0)
                with rasterio.open(tmp, 'w', **profile) as dst:
                    for y in range(0, src.height, 512):
                        for x in range(0, src.width, 512):
                            check_cancel(cancel)
                            w, h = min(512, src.width-x), min(512, src.height-y)
                            x0, y0 = max(0, x-48), max(0, y-48)
                            x1, y1 = min(src.width, x+w+48), min(src.height, y+h+48)
                            raw, valid = _read_analysis(src, info, Window(x0,y0,x1-x0,y1-y0), intensity=True)
                            features, mask = descriptors(raw[0], valid)
                            ys, xs = slice(y-y0,y-y0+h), slice(x-x0,x-x0+w)
                            window = Window(x,y,w,h)
                            dst.write(features[ys,xs].transpose(2,0,1), window=window)
                            dst.write_mask(mask[ys,xs].astype(np.uint8)*255, window=window)
                        if progress: progress(50*index+int(50*(y+h)/src.height), 'استخراج البنية متعددة المقاييس')
                    dst.update_tags(ANALYSIS_MODE=MODE, STRUCTURAL_VERSION='1', ANALYSIS_SOURCE=path)
                    dst.colorinterp = (rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green, rasterio.enums.ColorInterp.blue)
                    for band in range(1,4): dst.set_band_description(band, 'Structural gradient scale '+str(band))
            with rasterio.open(tmp) as check:
                for _, window in check.block_windows(1): check.read(window=window)
        check_cancel(cancel)
        for tmp, output in staged: os.replace(tmp, output)
        return dict(outputs=[out_a,out_b], mode=MODE)
    finally:
        for tmp, _ in staged:
            if os.path.exists(tmp): os.unlink(tmp)


def compare_features(a, b, valid_mask=None):
    if a.shape != b.shape or a.ndim != 3 or a.shape[2] != 3:
        raise ValueError('Structural comparison requires matching three-channel descriptor grids.')
    valid = np.all(a > 0, axis=2) & np.all(b > 0, axis=2)
    if valid_mask is not None: valid &= np.asarray(valid_mask) > 0
    # Descriptor differences only; never raw radiance or histogram matching.
    delta = np.mean(np.abs(a.astype(np.float32)-b.astype(np.float32)), axis=2)
    diff = cv2.GaussianBlur(delta, (0,0), 1.).clip(0,255).astype(np.uint8)
    valid = cv2.erode(valid.astype(np.uint8), np.ones((7,7),np.uint8)) > 0
    diff[~valid] = 0
    return diff, dict(mode=MODE, similarity_percent=float(100-diff[valid].mean()*100/255) if valid.any() else 0.,
                      radiometric_match=False, raw_pixel_difference=False, interpretation='Structural candidates require review')
