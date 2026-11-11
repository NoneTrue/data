import tempfile
import unittest
from pathlib import Path

from storage_manager import atomic_write, stage_output_path, commit_staged_output, validate_storage_path


class StorageManagerTests(unittest.TestCase):
    def test_unicode_path_is_writable_and_reports_free_space(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'قرص خارجي' / 'مسار عميق'
            result = validate_storage_path(target, create=True)
            self.assertTrue(target.is_dir())
            self.assertGreater(result['free_bytes'], 0)

    def test_atomic_write_keeps_existing_file_when_writer_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'result.txt'
            target.write_text('old', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                atomic_write(target, lambda temp: (_ for _ in ()).throw(RuntimeError('fail')), overwrite=True)
            self.assertEqual(target.read_text(encoding='utf-8'), 'old')
            self.assertEqual(list(Path(folder).glob('.result_*')), [])

    def test_atomic_write_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'result.txt'
            target.write_text('old', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                atomic_write(target, lambda temp: temp.write_text('new', encoding='utf-8'))
            self.assertEqual(target.read_text(encoding='utf-8'), 'old')

    def test_staged_output_is_committed_only_after_write_succeeds(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'result.tif'
            staged = stage_output_path(target)
            staged.write_bytes(b'new data')
            commit_staged_output(staged, target)
            self.assertEqual(target.read_bytes(), b'new data')
            self.assertFalse(staged.exists())


if __name__ == '__main__':
    unittest.main()
