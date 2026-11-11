import tempfile
from pathlib import Path
import unittest
import zipfile
from setup_optional_runtime import install_archive

class OptionalRuntimeSafetyTests(unittest.TestCase):
    def test_archive_root_and_existing_installation_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'component.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('Component/LICENSE', 'license')
                z.writestr('Component/bin/tool.txt', 'content')
            destination = root / 'installed'
            install_archive(archive, destination, 'Component')
            self.assertEqual((destination / 'bin/tool.txt').read_text(), 'content')
            with self.assertRaises(FileExistsError):
                install_archive(archive, destination, 'Component')
            self.assertEqual((destination / 'bin/tool.txt').read_text(), 'content')

    def test_zip_path_traversal_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'unsafe.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('../outside.txt', 'unsafe')
            with self.assertRaises(ValueError):
                install_archive(archive, root / 'installed', 'Component')
            self.assertFalse((root / 'outside.txt').exists())
            self.assertFalse((root / 'installed').exists())

    def test_case_colliding_names_are_rejected_on_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / 'duplicate.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('Component/A.txt', 'first')
                z.writestr('Component/a.txt', 'second')
            with self.assertRaises(ValueError):
                install_archive(archive, root / 'installed', 'Component')
            self.assertFalse((root / 'installed').exists())

if __name__ == '__main__':
    unittest.main()
