"""Regression checks: run with the project's Python 3.11 interpreter."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import main
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin
import engine
import gis_tools
import precise_registration


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(2026)
        gray = rng.integers(25, 230, (768, 768), dtype=np.uint8)
        gray = cv2.GaussianBlur(gray, (5, 5), 0.8)
        for _ in range(250):
            x, y = rng.integers(20, 748, 2)
            cv2.circle(gray, (int(x), int(y)), int(rng.integers(3, 12)),
                       int(rng.integers(30, 230)), -1)
        cls.rgb = np.repeat(gray[:, :, None], 3, axis=2)

    def test_geotiff_ycbcr_and_grid(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root = Path(folder)
            ref, src, out = [str(root / n) for n in ('ref.tif', 'src.tif', 'out.tif')]
            profile = dict(driver='GTiff', width=768, height=768, count=3,
                           dtype='uint8', crs='EPSG:32638', transform=from_origin(500000, 2800000, 0.5, 0.5),
                           compress='jpeg', photometric='YCBCR', tiled=True)
            for name in (ref, src):
                with rasterio.open(name, 'w', **profile) as ds:
                    ds.write(self.rgb.transpose(2, 0, 1))
            gis = gis_tools.inspect_pair(ref, src)
            self.assertEqual(gis['overlap']['percent_a'], 100)
            self.assertIn('Extent 1:', gis_tools.format_pair_report(gis))
            gis_tools.align_to_reference(ref, src, out)
            with rasterio.open(ref) as a, rasterio.open(out) as b:
                self.assertEqual((a.crs, a.transform, a.bounds, a.shape, a.res),
                                 (b.crs, b.transform, b.bounds, b.shape, b.res))
                self.assertEqual(b.read().shape, (3, 768, 768))
            self.assertEqual(engine.cv_imread_unicode(out).shape, self.rgb.shape)
            gis_tools.crop_overlap(ref, src, str(root/'crop1.tif'), str(root/'crop2.tif'))
            gis_tools.radiometric_match(ref, src, str(root/'radiometric.tif'))
            with rasterio.open(root/'radiometric.tif') as ds:
                self.assertEqual(ds.compression.value.upper(), 'LZW')
            precise_registration._warp_full_resolution(ref, src, str(root/'precise.tif'), np.eye(3)[:2])
            data = engine.read_image(ref)
            engine.save_geotiff(str(root/'rgb.tif'), self.rgb, data)
            engine.save_geotiff(str(root/'mask.tif'), self.rgb[:, :, 0], data)
            for name in root.glob('*.tif'):
                with rasterio.open(name) as ds:
                    ds.read()
                self.assertIsNotNone(engine.cv_imread_unicode(str(name)))
            with self.assertRaises(ValueError):
                gis_tools.align_to_reference(ref, src, src)

    def test_alignment_and_change_detection(self):
        transform = np.float32([[1, 0, 5.3], [0, 1, -3.7]])
        moving = cv2.warpAffine(self.rgb, transform, (768, 768))
        with patch.object(engine, '_smooth_mesh_residual_refine', side_effect=AssertionError('Unneeded mesh')):
            aligned, _, report, valid = engine.align_advanced(self.rgb, moving)
        print('ALIGNMENT METRICS:', report)
        self.assertTrue(report['alignment_approved'])
        self.assertLess(report['reprojection_error_px'], 1)
        self.assertLess(report['residual_p90_px'], 1.5)
        self.assertGreaterEqual(report['ecc_score'], .8)
        self.assertEqual(report['smooth_mesh_passes'], 0)
        altered = aligned.copy()
        altered[320:410, 320:410] = 245
        valid = engine.common_data_valid_mask(self.rgb, altered, valid)
        for method in ('SSIM', 'ROBUST FINAL'):
            diff, meta = engine.compare(self.rgb, altered, method, valid)
            mask = engine.change_mask(diff, 25, 30, 3, valid)
            self.assertGreater(np.count_nonzero(mask[330:400, 330:400]), 0)
            self.assertEqual(np.count_nonzero(mask[valid == 0]), 0)
        self.assertGreater(engine.alignment_quality(self.rgb, self.rgb), .99)

    def test_reject_unmeasurable_and_bad_residual(self):
        with self.assertRaises(RuntimeError):
            engine.align_advanced(np.zeros_like(self.rgb), self.rgb)
        bad = dict(count=16, median_dx=0., median_dy=0., median_mag=2.,
                   p90_mag=4., mad=2., shifts=[])
        with patch.object(engine, '_phase_tile_metrics', return_value=bad):
            with self.assertRaises(RuntimeError):
                engine.align_advanced(self.rgb, self.rgb)

    def test_nodata_and_ui_gate(self):
        difference = np.full((64, 64), 255, np.uint8)
        validity = difference.copy(); validity[:, 30:32] = 0
        mask = engine.change_mask(difference, 25, 1, 5, validity)
        self.assertFalse(mask[:, 30:32].any())
        with tempfile.TemporaryDirectory(dir='.') as folder:
            path = str(Path(folder)/'nodata.tif')
            arr = self.rgb.copy(); arr[:60] = 255
            with rasterio.open(path, 'w', driver='GTiff', width=768, height=768,
                               count=3, dtype='uint8', nodata=255,
                               crs='EPSG:32638', transform=from_origin(500000,2800000,1,1)) as dst:
                dst.write(arr.transpose(2, 0, 1))
            image = engine.read_image(path)
            valid = engine.common_data_valid_mask(image.array, self.rgb)
            self.assertFalse(valid[:60].any())
        app = main.QApplication.instance() or main.QApplication([])
        with tempfile.TemporaryDirectory(dir='.') as home, \
                patch.object(Path, 'home', return_value=Path(home).resolve()), \
                patch.object(main.MainWindow, '_cleanup_stale_temp_files'), \
                patch.object(main.MainWindow, '_save_report'):
            window = main.MainWindow()
            with self.assertRaises(RuntimeError):
                window._require_accurate_alignment()
            window.aligned = self.rgb
            window.last_alignment_report = {'alignment_approved': True}
            window._require_accurate_alignment()
            window._invalidate_registration()
            with self.assertRaises(RuntimeError):
                window._require_accurate_alignment()
            window.close()
        app.processEvents()

    def test_mesh_skips_excellent_alignment(self):
        with patch.object(engine, '_fit_smooth_residual_surface',
                          side_effect=AssertionError('Excellent alignment must skip mesh')):
            result = engine._smooth_mesh_residual_refine(
                self.rgb, self.rgb, np.full(self.rgb.shape[:2], 255, np.uint8), lambda *_: None)
        self.assertEqual(result[4], 0)

    def test_shifted_grid_mask_and_atomic_failure(self):
        with tempfile.TemporaryDirectory(dir='.') as folder:
            ref, src, output = [str(Path(folder)/n) for n in ('ref.tif','src.tif','out.tif')]
            profile = dict(driver='GTiff', width=96, height=96, count=3, dtype='uint8',
                           crs='EPSG:32638', transform=from_origin(500000, 2800000, 1, 1))
            with rasterio.open(ref, 'w', **profile) as ds:
                ds.write(np.full((3,96,96), 100, np.uint8))
            profile['transform'] = from_origin(500020, 2800000, 1, 1)
            with rasterio.open(src, 'w', **profile) as ds:
                ds.write(np.full((3,96,96), 150, np.uint8))
                mask = np.full((96,96),255,np.uint8); mask[30:40,30:40] = 0
                ds.write_mask(mask)
            gis_tools.align_to_reference(ref, src, output)
            with rasterio.open(output) as ds:
                mask = ds.dataset_mask()
                self.assertFalse(mask[:,:20].any())
                self.assertFalse(mask[30:40,50:60].any())
                self.assertTrue(mask[50:,30:].all())
            saved = Path(output).read_bytes()
            with patch.object(gis_tools, '_align_cpu', side_effect=RuntimeError('Simulated write failure')):
                with self.assertRaises(RuntimeError):
                    gis_tools.align_to_reference(ref, src, output)
            self.assertEqual(saved, Path(output).read_bytes())


if __name__ == '__main__':
    unittest.main(verbosity=2)
