import gzip
import json
import tempfile
import unittest
import shutil
import zipfile
from pathlib import Path

from project_format import read_project, write_project


class ProjectFormatTests(unittest.TestCase):
    def test_compressed_project_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'new.cvp'
            data = {'schema_version': 3, 'regions': [{'title': 'مبنى'}], 'analysis_mask_png': '00' * 2000}
            raw_size, packed_size = write_project(path, data)
            self.assertLess(packed_size, raw_size)
            self.assertEqual(read_project(path), data)
            self.assertEqual(gzip.decompress(path.read_bytes()), json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str).encode('utf-8'))

    def test_legacy_json_project_remains_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.cvp'
            expected = {'schema_version': 1, 'image1': 'before.tif', 'regions': []}
            path.write_text(json.dumps(expected), encoding='utf-8')
            self.assertEqual(read_project(path), expected)

    def test_embedded_project_reopens_after_source_assets_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'source files'; source.mkdir()
            before = source / 'صورة قبل.png'; after = source / 'image after.tif'
            before.write_bytes(b'BEFORE IMAGE BYTES')
            after.write_bytes(b'AFTER IMAGE BYTES')
            target_layer = source / 'target annotation.png'; target_layer.write_bytes(b'TARGET LAYER')
            project = root / 'self contained.cvp'
            data = {'schema_version': 4, 'image1': str(before), 'image2': str(after),
                    'regions': [{'title': 'هدف', 'image_layers': {'before': {'composite_path': str(target_layer)}}}]}
            write_project(project, data, include_assets=True)
            with zipfile.ZipFile(project) as archive:
                self.assertIn('project.json', archive.namelist())
                self.assertEqual(len([n for n in archive.namelist() if n.startswith('assets/')]), 3)
            shutil.rmtree(source)
            reopened = read_project(project)
            self.assertEqual(Path(reopened['image1']).read_bytes(), b'BEFORE IMAGE BYTES')
            self.assertEqual(Path(reopened['image2']).read_bytes(), b'AFTER IMAGE BYTES')
            layer = reopened['regions'][0]['image_layers']['before']['composite_path']
            self.assertEqual(Path(layer).read_bytes(), b'TARGET LAYER')


if __name__ == '__main__':
    unittest.main()
