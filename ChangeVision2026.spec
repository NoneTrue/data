"""Source checkout build. Optional development runtimes are not bundled."""
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

root = Path(SPECPATH)
datas, binaries, hidden = [], [], ['runtime_diagnostics', 'report_editor', 'report_docx', 'report_project']
for package in ('rasterio', 'pyproj', 'docx'):
    datas += collect_data_files(package)
    binaries += collect_dynamic_libs(package)
hidden += collect_submodules('rasterio', filter=lambda name: '.tests' not in name)
icon = root / 'runtime/icons/ChangeVision2026_HQ.ico'
a = Analysis([str(root / 'main.py')], pathex=[str(root)], binaries=binaries, datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[],
             excludes=['torch', 'kornia', 'kornia_rs', 'ultralytics', 'arcpy', 'qgis', 'osgeo',
                       'PyQt5', 'PyQt6', 'PySide2'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='ChangeVision2026',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, icon=str(icon))
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ChangeVision2026')
