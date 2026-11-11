import json
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from image_location import extract_image_metadata, reverse_geocode


class ImageLocationTests(unittest.TestCase):
    def test_jpeg_exif_gps_and_original_capture_date(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'photo.jpg'
            image = Image.new('RGB', (12, 8), 'white')
            exif = Image.Exif()
            exif[36867] = '2025:04:03 10:11:12'
            exif[34853] = {1: 'N', 2: (Fraction(24), Fraction(42), Fraction(0)),
                            3: 'E', 4: (Fraction(46), Fraction(43), Fraction(0))}
            image.save(path, exif=exif)
            result = extract_image_metadata(path)
            self.assertAlmostEqual(result['latitude'], 24.7, places=4)
            self.assertAlmostEqual(result['longitude'], 46.716666, places=4)
            self.assertEqual(result['capture_date'], '2025-04-03 10:11:12')
            self.assertEqual(result['location_kind'], 'camera_capture_point')

    def test_plain_image_does_not_invent_coordinates_or_city(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'no_location.png'
            Image.new('RGB', (5, 5), 'black').save(path)
            result = extract_image_metadata(path)
            self.assertIsNone(result['longitude'])
            self.assertIsNone(result['latitude'])
            self.assertEqual(result['location'], '')
            self.assertEqual(result['capture_date'], '')

    def test_geotiff_reports_coverage_centroid_not_capture_point(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scene.tif'
            with rasterio.open(path, 'w', driver='GTiff', width=10, height=10,
                               count=1, dtype='uint8', crs='EPSG:4326',
                               transform=from_origin(46.0, 25.0, 0.1, 0.1)) as dst:
                dst.write(np.zeros((1, 10, 10), dtype=np.uint8))
            result = extract_image_metadata(path)
            self.assertAlmostEqual(result['longitude'], 46.5)
            self.assertAlmostEqual(result['latitude'], 24.5)
            self.assertEqual(result['location_kind'], 'raster_coverage_centroid')
            self.assertIn('مركز تغطية', result['location'])
            self.assertEqual(result['bounds_wgs84'], [46.0, 24.0, 47.0, 25.0])

    def test_before_and_after_rasters_keep_separate_locations(self):
        with tempfile.TemporaryDirectory() as folder:
            results = []
            for name, west in (('before.tif', 40.0), ('after.tif', 46.0)):
                path = Path(folder) / name
                with rasterio.open(path, 'w', driver='GTiff', width=2, height=2,
                                   count=1, dtype='uint8', crs='EPSG:4326',
                                   transform=from_origin(west, 25.0, 0.1, 0.1)) as dst:
                    dst.write(np.zeros((1, 2, 2), dtype=np.uint8))
                results.append(extract_image_metadata(path))
            self.assertNotEqual(results[0]['longitude'], results[1]['longitude'])
            self.assertEqual(results[0]['location_kind'], 'raster_coverage_centroid')
            self.assertEqual(results[1]['location_kind'], 'raster_coverage_centroid')

    def test_reverse_geocoding_uses_cache_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache.json'
            cache.write_text(json.dumps({'24.70000,46.71667': {'display_name': 'مدينة اختبار'}}),
                             encoding='utf-8')
            result = reverse_geocode(46.716666, 24.7, cache)
            self.assertEqual(result['display_name'], 'مدينة اختبار')


if __name__ == '__main__':
    unittest.main()
