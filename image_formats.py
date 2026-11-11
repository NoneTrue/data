"""Format inspection and raster readers that preserve source files."""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import rasterio
from affine import Affine
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

import engine


COMMON_OPENCV = {'.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp',
                 '.ppm', '.pgm', '.pbm', '.pnm'}
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.gif', '.webp',
    '.jp2', '.j2k', '.j2c', '.jpc', '.ppm', '.pgm', '.pbm', '.pnm', '.svg',
    '.img', '.bil', '.bip', '.bsq', '.asc', '.grd', '.nc', '.h5', '.hdf',
    '.ecw', '.sid', '.vrt', '.dat', '.dem', '.pix', '.ers', '.ntf', '.nitf',
}


def gdal_raster_drivers():
    with rasterio.Env() as env:
        return env.drivers()


def _svg_renderer(path):
    renderer = QSvgRenderer(os.fspath(path))
    if not renderer.isValid():
        raise ValueError(f'ملف SVG تالف أو غير صالح: {path}')
    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        raise ValueError('أبعاد SVG غير صالحة.')
    return renderer, size


def _read_svg(path):
    renderer, size = _svg_renderer(path)
    image = QImage(size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    rgb_image = image.convertToFormat(QImage.Format_RGB888)
    ptr = rgb_image.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=rgb_image.sizeInBytes())
    arr = arr.reshape(rgb_image.height(), rgb_image.bytesPerLine())[:, :rgb_image.width() * 3]
    arr = arr.reshape(rgb_image.height(), rgb_image.width(), 3).copy()
    return engine.ImageData(os.fspath(path), arr, None, None, None)


def _read_gif(path):
    from PIL import Image
    with Image.open(path) as image:
        image.seek(0)
        rgb = np.asarray(image.convert('RGB')).copy()
    return engine.ImageData(os.fspath(path), rgb, None, None, None)


def _sidecar_georeference(path):
    path = Path(path)
    stem = path.with_suffix('')
    world_extensions = {
        '.tif': ('.tfw', '.tifw'), '.tiff': ('.tfw', '.tifw'),
        '.jpg': ('.jgw', '.jpgw'), '.jpeg': ('.jgw', '.jpgw'),
        '.png': ('.pgw', '.pngw'), '.bmp': ('.bpw', '.bmpw'),
        '.jp2': ('.j2w', '.jp2w'), '.gif': ('.gfw',),
    }
    candidates = [stem.with_suffix(ext) for ext in world_extensions.get(path.suffix.lower(), ())]
    candidates += [Path(str(path) + 'w'), stem.with_suffix('.wld')]
    transform = None
    for candidate in candidates:
        try:
            values = [float(line.strip()) for line in candidate.read_text(encoding='utf-8-sig').splitlines()[:6]]
            if len(values) == 6:
                a, d, b, e, c, f = values
                transform = Affine(a, b, c-(a+b)/2, d, e, f-(d+e)/2)
                break
        except (OSError, ValueError):
            continue
    crs = None
    prj = stem.with_suffix('.prj')
    if prj.is_file():
        try:
            text = prj.read_text(encoding='utf-8-sig')
            crs = rasterio.crs.CRS.from_wkt(text)
        except Exception:
            try:
                crs = rasterio.crs.CRS.from_string(prj.read_text(encoding='utf-8-sig'))
            except Exception:
                pass
    return crs, transform


def _read_rasterio(path):
    with rasterio.open(path) as src:
        if src.count < 1:
            raise ValueError('ملف الراستر لا يحتوي على نطاقات.')
        indexes = [1]
        colors = {str(color).lower(): index + 1 for index, color in enumerate(src.colorinterp)}
        if all(key in colors for key in ('red', 'green', 'blue')):
            indexes = [colors['red'], colors['green'], colors['blue']]
        elif src.count >= 3:
            indexes = [1, 2, 3]
        data = src.read(indexes=indexes, masked=True)
        raw = np.asarray(data.filled(0) if np.ma.isMaskedArray(data) else data)
        valid = ~np.any(np.ma.getmaskarray(data), axis=0) if np.ma.isMaskedArray(data) else src.dataset_mask() > 0
        arr = np.moveaxis(raw, 0, -1)
        rgb = engine._to_uint8(arr)
        if valid.shape == rgb.shape[:2]:
            rgb[~valid] = 0
        profile, transform, crs = src.profile.copy(), src.transform, src.crs
    return engine.ImageData(os.fspath(path), rgb, profile, transform, crs)


def read_analysis_image(path):
    """Read actual pixels for analysis; geospatial rasters retain their metadata."""
    path = os.fspath(path)
    suffix = Path(path).suffix.lower()
    if suffix == '.svg':
        return _read_svg(path)
    if suffix == '.gif':
        return _read_gif(path)
    if suffix in COMMON_OPENCV:
        try:
            data = engine.read_image(path)
        except Exception as exc:
            raise ValueError(str(exc)) from exc
        if suffix not in ('.tif', '.tiff'):
            crs, transform = _sidecar_georeference(path)
            if crs is not None or transform is not None:
                data.crs = data.crs or crs
                data.transform = data.transform or transform
                data.profile = {'driver': 'GTiff', 'height': data.height, 'width': data.width,
                                'count': 3, 'dtype': str(data.array.dtype),
                                'crs': data.crs, 'transform': data.transform}
        return data
    raster_error = None
    try:
        return _read_rasterio(path)
    except Exception as exc:
        raster_error = exc
    try:
        bgr = engine.cv_imread_unicode(path, cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        side_crs, side_transform = engine._recover_sidecar_georef(path)
        profile = None
        if side_crs is not None or side_transform is not None:
            profile = {'driver': 'GTiff', 'height': rgb.shape[0], 'width': rgb.shape[1],
                       'count': 3, 'dtype': str(rgb.dtype), 'crs': side_crs,
                       'transform': side_transform}
        return engine.ImageData(path, rgb, profile, side_transform, side_crs)
    except Exception as cv_error:
        raise ValueError(f'تعذر قراءة الصورة بواسطة GDAL/Rasterio وOpenCV.\nGDAL: {raster_error}\nOpenCV: {cv_error}') from cv_error


def inspect_image(path):
    """Open a candidate and report real content/metadata, not just its suffix."""
    path = Path(path)
    result = {'path': str(path), 'name': path.name, 'suffix': path.suffix.lower(),
              'bytes': path.stat().st_size if path.is_file() else 0,
              'readable': False, 'driver': '', 'width': '', 'height': '',
              'bands': '', 'dtype': '', 'bits': '', 'crs': '', 'nodata': '',
              'compatibility': 'غير مدعوم', 'details': ''}
    if not path.is_file():
        result['details'] = 'المسار ليس ملفًا.'
        return result
    if path.suffix.lower() == '.svg':
        try:
            _renderer, size = _svg_renderer(path)
            result.update(readable=True, driver='QtSvg (rasterization)', width=size.width(),
                          height=size.height(), bands=4, dtype='uint8', bits=8,
                          compatibility='جزئي', details='تحويل متجهي إلى صورة RGBA للعرض والتحليل؛ بلا CRS أو GeoMetadata.')
        except Exception as exc:
            result['details'] = str(exc)
        return result
    if path.suffix.lower() == '.gif':
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.seek(0)
                image.convert('RGB').load()
                result.update(readable=True, driver='Pillow', width=image.width, height=image.height,
                              bands=3, dtype='uint8', bits=8, compatibility='جزئي',
                              details=f'يعرض ويحلل الإطار الأول من {getattr(image,"n_frames",1)} إطارًا متاحًا؛ دون CRS.')
        except Exception as exc:
            result['details'] = f'GIF غير صالح: {exc}'
        return result
    try:
        with rasterio.open(path) as src:
            if src.width <= 0 or src.height <= 0 or src.count <= 0:
                raise ValueError('أبعاد الراستر أو عدد نطاقاته غير صالح.')
            from rasterio.windows import Window
            src.read(1, window=Window(0, 0, min(64, src.width), min(64, src.height)))
            side_crs, _side_transform = _sidecar_georeference(path)
            crs = src.crs or side_crs
            result.update(readable=True, driver=src.driver, width=src.width,
                          height=src.height, bands=src.count, dtype=', '.join(src.dtypes),
                          bits=max((np.dtype(dtype).itemsize * 8 for dtype in src.dtypes), default=0),
                          crs=str(crs) if crs else '',
                          nodata=', '.join('—' if value is None else str(value) for value in src.nodatavals))
            result['compatibility'] = 'جزئي'
            result['details'] = 'عينة البيانات وMetadata قُرئت؛ لم يكتمل تحقق جميع مراحل سير العمل لهذه الصيغة.'
            if src.count > 4:
                result['details'] = 'قراءة الراستر متاحة؛ مسار العرض والتحليل القياسي يستخدم حتى ثلاثة نطاقات RGB. عيّن الحزم عند الحاجة.'
            if result['bits'] > 8:
                result['details'] = (result['details'] + ' ' if result['details'] else '') + 'تُحوّل معاينة/مصفوفة التحليل الحالية إلى uint8 بتمديد مئيني؛ الأصل لا يتغير.'
            if not src.crs:
                result['details'] = (result['details'] + ' ' if result['details'] else '') + 'لا يوجد CRS مضمّن؛ افحص ملفات PRJ/World/AUX المرافقة.'
            return result
    except Exception as rio_error:
        try:
            image = engine.cv_imread_unicode(path, cv2.IMREAD_UNCHANGED)
            shape = image.shape
            result.update(readable=True, driver='OpenCV', width=shape[1], height=shape[0],
                          bands=1 if image.ndim == 2 else shape[2], dtype=str(image.dtype),
                          bits=image.dtype.itemsize * 8, compatibility='جزئي',
                          details='قراءة/عرض بكسلات دون CRS أو Metadata GIS مضمونة.')
        except Exception as cv_error:
            result['details'] = f'Rasterio: {rio_error}; OpenCV: {cv_error}'
    return result


class ImageImportDialog(QDialog):
    """Select several files or inspect a folder before assigning analysis inputs."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('استيراد الصور وفحص التوافق')
        self.resize(1250, 620)
        self.selected_path = None
        self.selected_role = None
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(['الملف', 'الصيغة/Driver', 'الأبعاد', 'النطاقات',
                                              'العمق', 'CRS', 'الحجم', 'الحالة', 'التفاصيل'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        tools = QHBoxLayout()
        for title, callback in (('اختيار ملفات متعددة…', self.choose_files),
                                ('اختيار مجلد…', self.choose_folder),
                                ('فتح كصورة قبل', lambda: self.assign('before')),
                                ('فتح كصورة بعد', lambda: self.assign('after'))):
            button = QPushButton(title)
            button.clicked.connect(callback)
            tools.addWidget(button)
        layout.addLayout(tools)
        self.status = QLabel('اختر الملفات أو مجلدًا؛ تُفحص البيانات الفعلية ويُعرض سبب عدم التوافق.')
        layout.addWidget(self.status)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'اختيار ملفات صور متعددة', '', 'All files (*);;Images (*)')
        if paths:
            self.inspect_paths(paths)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'اختيار مجلد صور')
        if folder:
            paths = [str(p) for p in Path(folder).rglob('*') if p.is_file()]
            self.inspect_paths(paths)

    def inspect_paths(self, paths):
        self.table.setRowCount(0)
        results = []
        for path in paths:
            try:
                info = inspect_image(path)
            except Exception as exc:
                info = {'path': path, 'name': Path(path).name, 'driver': '', 'width': '', 'height': '',
                        'bands': '', 'bits': '', 'crs': '', 'bytes': 0, 'compatibility': 'غير مدعوم',
                        'details': str(exc), 'readable': False}
            results.append(info)
        self.table.setRowCount(len(results))
        for row, info in enumerate(results):
            values = [info['name'], info['driver'],
                      f"{info['width']} × {info['height']}" if info['width'] != '' else '—',
                      info['bands'], info['bits'], info['crs'], f"{info['bytes']:,}",
                      'قابل للقراءة — دعم جزئي' if info['readable'] else 'غير مدعوم/تالف', info['details']]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(info.get('path', '')))
                self.table.setItem(row, col, item)
            self.table.item(row, 0).setData(Qt.UserRole, info['path'])
        self.status.setText(f'تم فحص {len(results)} ملفًا؛ القابل للقراءة: {sum(x["readable"] for x in results)}.')
        if results:
            self.table.selectRow(0)

    def assign(self, role):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, 'استيراد الصور', 'حدد ملفًا من القائمة أولًا.')
            return
        item = self.table.item(row, 0)
        path = item.data(Qt.UserRole) if item else None
        if not path:
            return
        self.selected_path, self.selected_role = path, role
        self.accept()
