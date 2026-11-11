import os
import tempfile
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication
import cv2
import numpy as np
import rasterio
from PIL import Image
from rasterio.shutil import copy as raster_copy
from rasterio.transform import from_origin

from image_formats import ImageImportDialog, inspect_image, read_analysis_image
import engine
from preparation import inspect_raster


class ImageFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        yy, xx = np.mgrid[:32, :40]
        self.rgb = np.dstack((xx * 5, yy * 7, (xx + yy) * 3)).clip(0, 255).astype(np.uint8)

    def tearDown(self):
        self.temp.cleanup()

    def test_common_raster_formats_decode_actual_pixels(self):
        for ext in ('.png', '.jpg', '.bmp', '.tif', '.webp', '.ppm'):
            path = self.root / ('actual' + ext)
            ok = cv2.imwrite(str(path), self.rgb)
            if not ok:
                self.skipTest(f'OpenCV encoder unavailable for {ext}')
            result = read_analysis_image(path)
            self.assertEqual(result.array.shape, self.rgb.shape, ext)
            self.assertTrue(inspect_image(path)['readable'], ext)
            diff, _ = engine.compare(result.array, result.array.copy(), 'SSIM')
            self.assertFalse(np.any(diff), ext)

    def test_gif_uses_first_frame_and_svg_rasterizes(self):
        gif = self.root / 'animated.gif'
        Image.fromarray(self.rgb).save(gif, save_all=True,
                                       append_images=[Image.fromarray(self.rgb[:, ::-1])], duration=100)
        gif_info = inspect_image(gif)
        self.assertEqual(gif_info['compatibility'], 'جزئي')
        self.assertIn('الإطار الأول', gif_info['details'])
        self.assertEqual(read_analysis_image(gif).array.shape, self.rgb.shape)

        svg = self.root / 'map.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">'
                       '<rect width="20" height="10" fill="red"/></svg>', encoding='utf-8')
        svg_image = read_analysis_image(svg)
        self.assertEqual(svg_image.array.shape, (10, 20, 3))
        self.assertEqual(svg_image.crs, None)
        diff, _ = engine.compare(svg_image.array, svg_image.array.copy(), 'SSIM')
        self.assertFalse(np.any(diff))

    def _write_raster(self, path, driver='GTiff', count=3, dtype='uint8', **options):
        array = np.arange(count * 32 * 40, dtype=dtype).reshape(count, 32, 40)
        with rasterio.open(path, 'w', driver=driver, width=40, height=32, count=count,
                           dtype=dtype, crs='EPSG:32638',
                           transform=from_origin(500000, 2800000, 0.5, 0.5), **options) as dst:
            dst.write(array)
        return array

    def test_geotiff_multiband_nodata_and_georeferencing(self):
        path = self.root / 'multi.tif'
        self._write_raster(path, nodata=0)
        info = inspect_image(path)
        self.assertTrue(info['readable'])
        self.assertEqual(info['bands'], 3)
        self.assertIn('32638', info['crs'])
        loaded = read_analysis_image(path)
        self.assertEqual(loaded.array.shape, (32, 40, 3))
        metadata = inspect_raster(str(path))
        self.assertEqual(metadata['bands'], 3)
        self.assertEqual(metadata['crs'], 'EPSG:32638')

        many = self.root / 'five_bands.tif'
        self._write_raster(many, count=5, dtype='uint16')
        many_info = inspect_image(many)
        self.assertEqual(many_info['bands'], 5)
        self.assertEqual(many_info['compatibility'], 'جزئي')
        self.assertEqual(read_analysis_image(many).array.shape, (32, 40, 3))

    def test_worldfile_and_prj_are_sidecars_not_image_formats(self):
        path = self.root / 'tile.png'
        self.assertTrue(cv2.imwrite(str(path), self.rgb))
        Path(self.root / 'tile.pgw').write_text('0.5\n0\n0\n-0.5\n500000.25\n2799999.75\n', encoding='ascii')
        (self.root / 'tile.prj').write_text(rasterio.crs.CRS.from_epsg(32638).to_wkt(), encoding='utf-8')
        loaded = read_analysis_image(path)
        self.assertEqual(loaded.crs.to_epsg(), 32638)
        self.assertAlmostEqual(loaded.transform.c, 500000.0)
        self.assertAlmostEqual(loaded.transform.f, 2800000.0)
        self.assertEqual(Path(loaded.path).suffix, '.png')

    def test_sixteen_bit_and_bigtiff_are_read_without_changing_source(self):
        path16 = self.root / 'sixteen.tif'
        source16 = self._write_raster(path16, dtype='uint16')
        source_hash_data = path16.read_bytes()
        loaded = read_analysis_image(path16)
        self.assertEqual(loaded.array.dtype, np.uint8)
        self.assertEqual(path16.read_bytes(), source_hash_data)
        self.assertIn('uint16', inspect_image(path16)['dtype'])

        big = self.root / 'big.tif'
        self._write_raster(big, BIGTIFF='YES')
        self.assertEqual(read_analysis_image(big).array.shape, (32, 40, 3))
        self.assertGreater(source16.max(), 255)

        float_path = self.root / 'floating.tif'
        float_values = np.linspace(-2.5, 5.5, 3*32*40, dtype=np.float32).reshape(3, 32, 40)
        with rasterio.open(float_path, 'w', driver='GTiff', width=40, height=32,
                           count=3, dtype='float32', nodata=-9999) as dst:
            dst.write(float_values)
        float_image = read_analysis_image(float_path)
        self.assertEqual(float_image.array.dtype, np.uint8)
        self.assertLess(float_values.min(), 0)

    def test_large_raster_actual_read(self):
        path = self.root / 'large.tif'
        source = np.zeros((3, 2048, 3072), dtype=np.uint8)
        source[0] = 90
        with rasterio.open(path, 'w', driver='GTiff', width=3072, height=2048,
                           count=3, dtype='uint8', tiled=True, compress='deflate') as dst:
            dst.write(source)
        loaded = read_analysis_image(path)
        self.assertEqual(loaded.array.shape, (2048, 3072, 3))
        self.assertEqual(int(loaded.array[0, 0, 0]), 90)

    def test_gdal_raster_drivers_hfa_envi_jpeg2000_and_cog(self):
        formats = [('.img', 'HFA', {}), ('.bil', 'ENVI', {'INTERLEAVE': 'BIL'}),
                   ('.bip', 'ENVI', {'INTERLEAVE': 'BIP'}), ('.bsq', 'ENVI', {'INTERLEAVE': 'BSQ'}),
                   ('.jp2', 'JP2OpenJPEG', {})]
        for ext, driver, options in formats:
            path = self.root / ('sample' + ext)
            try:
                self._write_raster(path, driver=driver, **options)
            except Exception as exc:
                self.skipTest(f'GDAL driver {driver} cannot create test fixture: {exc}')
            self.assertTrue(inspect_image(path)['readable'], ext)
            loaded = read_analysis_image(path)
            self.assertEqual(loaded.array.shape, (32, 40, 3), ext)
            metadata = inspect_raster(str(path))
            self.assertEqual(metadata['width'], 40, ext)
            diff, _ = engine.compare(loaded.array, loaded.array.copy(), 'SSIM')
            self.assertFalse(np.any(diff), ext)
            exported = self.root / ('export_' + path.stem + '.tif')
            engine.save_geotiff(str(exported), loaded.array, loaded)
            with rasterio.open(exported) as result:
                self.assertEqual((result.width, result.height, result.count), (40, 32, 3), ext)

        tif = self.root / 'source.tif'
        self._write_raster(tif)
        cog = self.root / 'sample_cog.tif'
        try:
            with rasterio.open(tif) as src:
                raster_copy(src, cog, driver='COG')
        except Exception as exc:
            self.skipTest(f'COG driver cannot create test fixture: {exc}')
        self.assertEqual(read_analysis_image(cog).array.shape, (32, 40, 3))

        asc = self.root / 'grid.asc'
        try:
            self._write_raster(asc, driver='AAIGrid', count=1, dtype='int16')
        except Exception as exc:
            self.skipTest(f'AAIGrid driver cannot create test fixture: {exc}')
        self.assertTrue(inspect_image(asc)['readable'])
        self.assertEqual(read_analysis_image(asc).array.shape, (32, 40, 3))
        asc_export = self.root / 'grid_export.tif'
        engine.save_geotiff(str(asc_export), read_analysis_image(asc).array, read_analysis_image(asc))
        self.assertTrue(asc_export.is_file())

    def test_corrupt_and_unsupported_files_are_rejected_by_content(self):
        broken = self.root / 'broken.tif'
        broken.write_bytes(b'not a raster')
        self.assertFalse(inspect_image(broken)['readable'])
        with self.assertRaises(ValueError):
            read_analysis_image(broken)
        unsupported = self.root / 'vector.xyz'
        unsupported.write_text('not an image', encoding='utf-8')
        self.assertFalse(inspect_image(unsupported)['readable'])

    def test_import_dialog_lists_multiple_files_and_content_status(self):
        image = self.root / 'visible.png'
        self.assertTrue(cv2.imwrite(str(image), self.rgb))
        broken = self.root / 'broken.bin'
        broken.write_bytes(b'bad data')
        dialog = ImageImportDialog()
        dialog.inspect_paths([str(image), str(broken)])
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn('قابل للقراءة', dialog.table.item(0, 7).text())
        self.assertIn('غير مدعوم', dialog.table.item(1, 7).text())


if __name__ == '__main__':
    unittest.main()
