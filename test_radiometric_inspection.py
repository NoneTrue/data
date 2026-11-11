"""Read-only radiometric inspection regression tests (Python 3.11)."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import main  # Initialize Qt DLLs before Rasterio on Windows.
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QCloseEvent
import radiometric_inspection as inspection


class RadiometricInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(12)
        gray = rng.integers(25, 160, (384, 384), dtype=np.uint8)
        gray = cv2.GaussianBlur(gray, (3, 3), .6)
        for _ in range(150):
            x, y = rng.integers(10, 374, 2)
            cv2.circle(gray, (int(x), int(y)), int(rng.integers(2, 8)),
                       int(rng.integers(25, 160)), -1)
        cls.rgb = np.repeat(gray[None, :, :], 3, axis=0)
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.write('a.tif', self.rgb)

    def write(self, name, data, nodata=None, transform=None, mask=None):
        path = self.root/name
        with rasterio.open(path, 'w', driver='GTiff', width=data.shape[2], height=data.shape[1],
                           count=data.shape[0], dtype=data.dtype, nodata=nodata,
                           crs='EPSG:32638', transform=transform or from_origin(500000, 2800000, .5, .5)) as ds:
            ds.write(data)
            if mask is not None:
                ds.write_mask(mask)
        return str(path)

    def approved(self, H=None, valid=None, aligned=None, mesh=0):
        return {'report': {'alignment_approved': True, 'smooth_mesh_passes': mesh, 'model': 'AFFINE'},
                'H': np.eye(3) if H is None else H,
                'valid_mask': np.full((384,384),255,np.uint8) if valid is None else valid,
                'aligned': aligned}

    def test_similar_and_brightness_with_geometry_measurement(self):
        for name, image, expected in (
                ('similar.tif', self.rgb + 1, False),
                ('bright.tif', np.clip(self.rgb.astype(float) * 1.25 + 10, 0, 255).astype(np.uint8), True)):
            with self.subTest(name=name):
                b = self.write(name, image)
                before = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (self.a, b)]
                with patch.object(main.gis_tools, 'radiometric_match', side_effect=AssertionError('Must not match')), \
                        patch.object(main.engine, 'align_advanced', side_effect=AssertionError('Must not align')):
                    result = inspection.inspect_radiometry(self.a, b)
                self.assertEqual(result['recommend_match'], expected)
                self.assertEqual(result['geometry']['source'], 'measured')
                self.assertFalse(result['radiometric_applied'])
                self.assertEqual(before, [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (self.a, b)])
                self.assertEqual(len(result['bands']), 3)
                self.assertAlmostEqual(result['bands'][0]['image1']['mean'], self.rgb[0].mean())
                self.assertIn('Recommendation:', inspection.format_report(result))
                print(name, {k: result[k] for k in ('brightness_difference_percent',
                                                    'color_difference_percent', 'histogram_difference_percent',
                                                    'recommend_match')})

    def test_color_cast_and_native_multiband_statistics(self):
        tinted = self.rgb.copy(); tinted[0] += 50
        b = self.write('tinted.tif', tinted)
        result = inspection.inspect_radiometry(self.a, b, alignment=self.approved())
        self.assertTrue(result['recommend_match'])
        self.assertGreater(result['color_difference_percent'], 5)
        native = np.concatenate((self.rgb.astype(np.uint16)*100,
                                 self.rgb[:1].astype(np.uint16)*150))
        a, b = self.write('native1.tif', native), self.write('native2.tif', native)
        result = inspection.inspect_radiometry(a, b, alignment=self.approved())
        self.assertEqual(len(result['bands']), 4)
        self.assertAlmostEqual(result['bands'][3]['image1']['mean'], native[3].mean())
        self.assertFalse(result['recommend_match'])

    def test_nodata_black_masks_and_no_overlap(self):
        a, b = self.rgb.copy(), self.rgb.copy()
        a[:, :32] = 255  # Declared NoData.
        b[:, 32:64] = 0  # Black even though declared NoData is 255.
        b[:, 64:96] = 240  # Excluded by explicit dataset mask.
        mask = np.full((384,384),255,np.uint8); mask[64:96] = 0
        p = self.write('nodata1.tif', a, nodata=255)
        q = self.write('nodata2.tif', b, nodata=255, mask=mask)
        valid = np.full((384,384),255,np.uint8); valid[96:128] = 0
        result = inspection.inspect_radiometry(p, q, alignment=self.approved(valid=valid))
        self.assertEqual(result['valid_samples'], 256*384)
        self.assertEqual(result['brightness_difference_percent'], 0)
        self.assertEqual(result['histogram_difference_percent'], 0)
        with self.assertRaises(RuntimeError):
            inspection.inspect_radiometry(p, q, alignment=self.approved(valid=np.zeros_like(valid)))

    def test_grid_and_geometry_requirements_and_aligned_result(self):
        shifted_grid = self.write('grid.tif', self.rgb, transform=from_origin(500001, 2800000, .5, .5))
        with self.assertRaisesRegex(RuntimeError, 'CRS'):
            inspection.inspect_radiometry(self.a, shifted_grid)
        moving = cv2.warpAffine(self.rgb.transpose(1,2,0), np.float32([[1,0,7],[0,1,0]]), (384,384))
        b = self.write('shift.tif', moving.transpose(2,0,1))
        with self.assertRaisesRegex(RuntimeError, 'المواءمة'):
            inspection.inspect_radiometry(self.a, b)
        H = np.array([[1,0,-7],[0,1,0],[0,0,1]],float)
        result = inspection.inspect_radiometry(self.a, b, alignment=self.approved(H=H))
        self.assertFalse(result['recommend_match'])
        self.assertEqual(result['brightness_difference_percent'], 0)
        # Use the actual aligned image after a mesh, rather than the unaligned file.
        result = inspection.inspect_radiometry(self.a, b, alignment=self.approved(
            aligned=self.rgb.transpose(1,2,0).copy(), mesh=1))
        self.assertFalse(result['recommend_match'])

    def test_worker_button_success_failure_and_close(self):
        b = self.write('same.tif', self.rgb)
        with patch.object(Path, 'home', return_value=self.root), \
                patch.object(main.MainWindow, '_cleanup_stale_temp_files'), \
                patch.object(main.MainWindow, '_save_report'), \
                patch.object(main.MainWindow, 'err') as errors, \
                patch.object(main.QMessageBox, 'exec', return_value=main.QMessageBox.Ok), \
                patch.object(main.gis_tools, 'radiometric_match', side_effect=AssertionError('Must not match')):
            window = main.MainWindow()
            self.addCleanup(window.close)
            window._load_a(self.a); window._load_b(b)
            button = window.gis_inspect_colors_button
            self.assertEqual(button.text(), 'فحص الإضاءة والألوان')
            layout = button.parentWidget().layout()
            self.assertEqual(layout.itemAt(layout.indexOf(button)+1).widget().text(),
                             'توحيد الإضاءة والألوان (Radiometric / Histogram Match)')
            for fail in (False, True):
                if fail:
                    window.b.path = str(self.root/'missing.tif')
                button.click()
                self.assertFalse(button.isEnabled())
                event = QCloseEvent(); window.closeEvent(event)
                self.assertFalse(event.isAccepted())
                worker = window._radiometric_inspect_worker
                loop = QEventLoop()
                finished = []
                worker.finished.connect(lambda: finished.append(True))
                worker.finished.connect(loop.quit)
                QTimer.singleShot(30000, loop.quit)
                loop.exec()
                # The application may already have deleted the finished Qt
                # worker; inspect its completion signal, not its C++ wrapper.
                if not finished:
                    worker.wait(30000)
                self.assertTrue(finished, 'Worker did not finish before timeout')
                self.app.processEvents()
                self.assertTrue(button.isEnabled())
                if not fail:
                    self.assertIn(inspection.SIMILAR, window.stats_detail.toPlainText())
                    errors.assert_not_called()
                else:
                    errors.assert_called_once()
            window.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
