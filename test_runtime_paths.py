import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import main
import unittest,tempfile,shutil,subprocess,sys,json,ast
from pathlib import Path
from unittest.mock import patch
import runtime_paths as paths


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=main.QApplication.instance() or main.QApplication([])

    def test_frozen_external_runtime_separate_from_meipass(self):
        with tempfile.TemporaryDirectory(dir='.') as d:
            root=Path(d).resolve()/'Install With Spaces'
            with patch.object(sys,'frozen',True,create=True),patch.object(sys,'executable',str(root/'ChangeVision2026.exe')),patch.object(sys,'_MEIPASS',str(root/'_internal'),create=True):
                self.assertEqual(paths.get_runtime_path(),root/'runtime')
                self.assertEqual(paths.get_bundle_path(),root/'_internal')

    def test_optional_absence_icon_and_writable_outputs(self):
        with tempfile.TemporaryDirectory(dir='.') as d,patch.object(main.MainWindow,'_cleanup_stale_temp_files'),patch.object(main.MainWindow,'_save_report'):
            root=Path(d).resolve()
            with patch.object(paths,'get_runtime_path',return_value=root/'absent'),patch.object(Path,'home',return_value=root):
                window=main.MainWindow()
                self.assertFalse(paths.apply_application_icon(None))
                for name in ('OTB','GeoAlignAI','Models','Icons','Assets'):
                    self.assertEqual(window.runtime_dependencies[name]['status'],'MISSING')
                generated=Path(window._new_analysis_path('check'))
                self.assertTrue(generated.is_relative_to(root/'Documents'))
                window.close()

    def test_package_data_and_environment_isolation(self):
        self.assertTrue(paths.get_gdal_data_path().is_dir())
        for provider in ('rasterio','pyproj'):self.assertTrue((paths.get_proj_data_path(provider)/'proj.db').is_file())
        before=os.environ.copy();env=paths.get_otb_environment()
        self.assertEqual(os.environ,before)
        self.assertTrue(env['PATH'].startswith(str(paths.get_otb_path()/'bin')))
        self.assertNotEqual(env['PROJ_DATA'],os.environ['PROJ_DATA'])

    def test_moved_source_with_spaces_and_absent_optional(self):
        root=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(dir=root) as d:
            moved=Path(d)/'Moved Project With Spaces';moved.mkdir()
            for p in root.glob('*.py'):
                if not p.name.startswith('test_'):shutil.copy2(p,moved/p.name)
            shutil.copytree(root/'runtime'/'icons',moved/'runtime'/'icons')
            output=moved/'diagnostic.json';env=os.environ.copy();env['QT_QPA_PLATFORM']='offscreen'
            cp=subprocess.run([sys.executable,'-X','utf8',str(moved/'main.py'),'--runtime-diagnostics',str(output)],cwd=d,
                              env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
            self.assertEqual(cp.returncode,0,cp.stderr)
            result=json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(result['smoke'],'PASS');self.assertTrue(result['window_icon'])
            self.assertEqual(result['dependencies']['OTB']['status'],'MISSING')
            self.assertEqual(result['dependencies']['GeoAlignAI']['status'],'MISSING')

    def test_production_paths_and_syntax(self):
        root=Path(__file__).resolve().parent
        for p in root.glob('*.py'):
            text=p.read_text(encoding='utf-8-sig');tree=ast.parse(text)
            compile(tree,str(p),'exec')
            if p.name.startswith('test_'):continue
            for n in ast.walk(tree):
                if isinstance(n,ast.Constant) and isinstance(n.value,str):
                    self.assertNotIn('C:\\Users\\TRK',n.value,(p.name,n.lineno))


if __name__=='__main__':unittest.main()
