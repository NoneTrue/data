"""Portable resources and per-user output paths; no analysis code lives here."""
from pathlib import Path
import importlib.util
import os
import sys


def get_app_base_path():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent


def get_bundle_path():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def get_runtime_path(): return get_app_base_path() / 'runtime'
def get_otb_path(): return get_runtime_path() / 'OTB-9.1.0-Win64'
def get_geoalignai_path(): return get_runtime_path() / 'GeoAlignAI'
def get_models_path(): return get_runtime_path() / 'models'
def get_icons_path(): return get_runtime_path() / 'icons'
def get_main_icon_path(): return get_icons_path() / 'ChangeVision2026_HQ.ico'
def get_assets_path(): return get_runtime_path() / 'assets'
def get_storage_path(category):
    from storage_manager import get_storage_path as configured_storage_path
    return configured_storage_path(category)


def get_user_data_path():
    """Backward-compatible accessor for the configurable main storage folder."""
    return get_storage_path('root')


def _package_root(name):
    spec = importlib.util.find_spec(name)
    if spec and spec.origin:
        return Path(spec.origin).parent
    return get_bundle_path() / name


def get_gdal_data_path():
    return _package_root('rasterio') / 'gdal_data'


def get_proj_data_path(provider='pyproj'):
    if provider == 'rasterio': return _package_root('rasterio') / 'proj_data'
    return _package_root('pyproj') / 'proj_dir' / 'share' / 'proj'


def configure_geospatial_environment():
    """Use each wheel's matching data, overriding inherited QGIS/OTB data."""
    gdal = get_gdal_data_path(); rio_proj = get_proj_data_path('rasterio')
    if gdal.is_dir(): os.environ['GDAL_DATA'] = str(gdal)
    if (rio_proj / 'proj.db').is_file():
        os.environ['PROJ_DATA'] = str(rio_proj)
        os.environ['PROJ_LIB'] = str(rio_proj)
    # Never load third-party GDAL plugins into Rasterio's private GDAL.
    os.environ['GDAL_DRIVER_PATH'] = 'disable'
    import pyproj
    own_proj = get_proj_data_path()
    if (own_proj / 'proj.db').is_file(): pyproj.datadir.set_data_dir(str(own_proj))


def get_otb_environment():
    """Environment for OTB child processes only; leave application DLLs alone."""
    root = get_otb_path(); env = os.environ.copy()
    env['PATH'] = os.pathsep.join([str(root/'bin'),str(root/'lib'),env.get('PATH','')])
    env['OTB_APPLICATION_PATH'] = str(root/'lib'/'otb'/'applications')
    env['GDAL_DATA'] = str(root/'share'/'data')
    env['PROJ_LIB'] = env['PROJ_DATA'] = str(root/'share'/'proj')
    env['GDAL_DRIVER_PATH'] = 'disable'; env['LC_NUMERIC'] = 'C'
    env.pop('PYTHONHOME',None);env.pop('PYTHONPATH',None)
    return env


def get_geoalignai_environment():
    env = os.environ.copy()
    env['GEOALIGNAI_HOME'] = str(get_geoalignai_path())
    vendor=get_geoalignai_path()/'python_packages'
    if vendor.is_dir():
        env['PYTHONPATH']=os.pathsep.join(filter(None,(str(vendor),env.get('PYTHONPATH',''))))
    # Repositories have conflicting top-level `models` modules. Do not add
    # all of them to sys.path or silently select a new alignment backend.
    return env


def dependency_status():
    checks = [('OTB',get_otb_path()/'bin'/'otbApplicationLauncherCommandLine.exe',True),
              ('GeoAlignAI',get_geoalignai_path()/'repos',True),
              ('Models',get_models_path(),True),('Icons',get_main_icon_path(),True),
              ('Assets',get_assets_path(),True),('GDAL_DATA',get_gdal_data_path(),False),
              ('Rasterio_PROJ_DATA',get_proj_data_path('rasterio')/'proj.db',False),
              ('PyProj_PROJ_DATA',get_proj_data_path()/'proj.db',False)]
    return {name:dict(path=str(path),status='FOUND' if path.exists() else 'MISSING',
                      requirement='OPTIONAL' if optional else 'REQUIRED') for name,path,optional in checks}


def apply_application_icon(app, window=None):
    from PySide6.QtGui import QIcon
    icon = QIcon(str(get_main_icon_path())) if get_main_icon_path().is_file() else QIcon()
    if icon.isNull():
        fallback=get_icons_path()/'ChangeVision2026_256x256.png'
        if fallback.is_file():icon=QIcon(str(fallback))
    if not icon.isNull():
        if app:app.setWindowIcon(icon)
        if window:window.setWindowIcon(icon)
    return not icon.isNull()


def set_windows_app_id():
    if sys.platform == 'win32':
        import ctypes
        try:ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('ChangeVision2026.Analysis')
        except (AttributeError,OSError):pass
