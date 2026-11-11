"""Central user-configurable storage paths and safe output helpers."""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)


APP_ORG = 'ChangeVision2026'
APP_NAME = 'AnalysisEdition'

STORAGE_LABELS = {
    'reports': 'Reports',
    'root': 'مجلد الحفظ الرئيسي',
    'analysis_results': 'نتائج التحليل',
    'excel': 'تقارير Excel',
    'word': 'تقارير Word',
    'pdf': 'تقارير PDF',
    'source_images': 'الصور الأصلية (مسار مرجعي فقط)',
    'processed_images': 'الصور المعالجة',
    'illustrative_images': 'الصور التوضيحية',
    'projects': 'مشاريع CVP',
    'gis': 'ملفات GIS',
    'geotiff': 'ملفات GeoTIFF',
    'geojson': 'ملفات GeoJSON',
    'backups': 'النسخ الاحتياطية',
    'exports': 'ملفات التصدير',
    'attachments': 'المرفقات',
}


def _default_root():
    return Path.home() / 'Documents' / APP_ORG


def default_storage_path(category):
    configured_root = _settings().value('storage/root', '', type=str) if category != 'root' else ''
    root = Path(configured_root).expanduser() if configured_root else _default_root()
    relative = {
        'reports': Path('Reports'),
        'root': Path(),
        'analysis_results': Path('AnalysisResults'),
        'excel': Path('Reports') / 'Excel',
        'word': Path('Reports') / 'Word',
        'pdf': Path('Reports') / 'PDF',
        'source_images': Path('Images') / 'Originals',
        'processed_images': Path('Images') / 'Processed',
        'illustrative_images': Path('Images') / 'Illustrative',
        'projects': Path('Projects'),
        'gis': Path('GIS'),
        'geotiff': Path('GIS') / 'GeoTIFF',
        'geojson': Path('GIS') / 'GeoJSON',
        'backups': Path('Backups'),
        'exports': Path('Exports'),
        'attachments': Path('Attachments'),
    }
    if category not in relative:
        raise KeyError(f'Unknown storage category: {category}')
    return root / relative[category]


def _settings():
    return QSettings(APP_ORG, APP_NAME)


def get_storage_path(category):
    configured = _settings().value(f'storage/{category}', '', type=str)
    return Path(configured).expanduser() if configured else default_storage_path(category)


def set_storage_path(category, path):
    if category not in STORAGE_LABELS:
        raise KeyError(f'Unknown storage category: {category}')
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise ValueError('اختر مسارًا كاملًا يتضمن القرص أو مسار الشبكة.')
    if category == 'source_images':
        if not target.is_dir():
            raise FileNotFoundError(f'مجلد الصور المرجعي غير متاح: {target}')
    else:
        validate_storage_path(target, create=True)
    value = str(target)
    _settings().setValue(f'storage/{category}', value)
    _settings().sync()
    return Path(value)


def reset_storage_paths():
    settings = _settings()
    for category in STORAGE_LABELS:
        settings.remove(f'storage/{category}')
    settings.sync()


def validate_storage_path(path, *, create=False, minimum_free_bytes=0, probe_write=True):
    """Validate drive, directory, available space, and actual write permission."""
    target = Path(path).expanduser()
    if not str(target).strip():
        raise ValueError('مسار الحفظ فارغ.')
    from runtime_paths import get_app_base_path
    resolved = target.resolve()
    application = get_app_base_path().resolve()
    if resolved == application or application in resolved.parents:
        raise PermissionError('لا يمكن حفظ بيانات المستخدم داخل مجلد البرنامج. اختر مجلدًا آخر خارج مسار التثبيت.')
    if target.anchor and not Path(target.anchor).is_dir():
        raise FileNotFoundError(f'القرص أو مورد التخزين غير متاح: {target.anchor}')
    if create:
        target.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        raise FileNotFoundError(f'المجلد أو القرص غير متاح: {target}')
    if not target.is_dir():
        raise NotADirectoryError(f'مسار الحفظ ليس مجلدًا: {target}')
    usage = shutil.disk_usage(target)
    if usage.free < int(minimum_free_bytes):
        raise OSError(f'المساحة المتاحة غير كافية على {target.anchor or target}: {usage.free:,} bytes.')
    if probe_write:
        probe = None
        try:
            candidate = target / f'.changevision_write_test_{uuid.uuid4().hex}'
            with candidate.open('xb') as stream:
                probe = candidate
                stream.write(b'ChangeVision storage check')
        except OSError as exc:
            raise PermissionError(f'لا توجد صلاحية كتابة في: {target} ({exc})') from exc
        finally:
            if probe is not None:
                try:
                    probe.unlink(missing_ok=True)
                except OSError:
                    pass
    return {'path': target, 'free_bytes': usage.free, 'total_bytes': usage.total}


def validate_output_path(path, *, minimum_free_bytes=0, create_parent=True):
    output = Path(path).expanduser()
    parent = output.parent
    validate_storage_path(parent, create=create_parent,
                          minimum_free_bytes=minimum_free_bytes, probe_write=True)
    return output


def atomic_write(path, writer, *, overwrite=False):
    """Write a file beside its destination and replace only after success."""
    target = validate_output_path(path, create_parent=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f'يوجد ملف بالفعل: {target}')
    fd, temp_name = tempfile.mkstemp(prefix=f'.{target.stem}_', suffix=target.suffix,
                                     dir=str(target.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        writer(temp_path)
        if not temp_path.is_file():
            raise OSError('لم تنشئ عملية الكتابة ملفًا صالحًا.')
        os.replace(temp_path, target)
        return target
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def stage_output_path(path):
    """Return a unique sibling path for a long-running writer, preserving suffix."""
    target = validate_output_path(path, create_parent=True)
    return target.with_name(f'.{target.stem}_{uuid.uuid4().hex}.tmp{target.suffix}')


def commit_staged_output(staged_path, final_path, *, overwrite=False):
    staged = Path(staged_path)
    target = validate_output_path(final_path, create_parent=True)
    if not staged.is_file():
        raise FileNotFoundError(f'لم يُنشأ الملف المؤقت: {staged}')
    if target.exists() and not overwrite:
        raise FileExistsError(f'يوجد ملف بالفعل: {target}')
    os.replace(staged, target)
    return target


def confirm_overwrite(parent, path):
    output = Path(path)
    if not output.exists():
        return True
    return QMessageBox.question(
        parent, 'تأكيد الاستبدال', f'الملف موجود بالفعل:\n{output}\n\nهل تريد استبداله؟',
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes


class StorageSettingsDialog(QDialog):
    """Settings page for independently configurable project output locations."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('إدارة التخزين ومسارات الحفظ')
        self.resize(950, 680)
        root = QVBoxLayout(self)
        note = QLabel('تُحفظ الإعدادات تلقائيًا. الصور الأصلية تبقى في مواقعها؛ هذا المسار مرجعي فقط.')
        note.setWordWrap(True)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        form = QFormLayout(body)
        self.fields = {}
        for category, label in STORAGE_LABELS.items():
            row = QHBoxLayout()
            edit = QLineEdit(str(get_storage_path(category)))
            self.fields[category] = edit
            edit.editingFinished.connect(lambda key=category: self._save(key))
            browse = QPushButton('استعراض…')
            browse.clicked.connect(lambda _=False, key=category: self._browse(key))
            open_button = QPushButton('فتح المجلد')
            open_button.clicked.connect(lambda _=False, key=category: self._open(key))
            row.addWidget(edit, 1)
            row.addWidget(browse)
            row.addWidget(open_button)
            form.addRow(label, row)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        self.status = QLabel('')
        buttons.addWidget(self.status, 1)
        reset = QPushButton('استعادة الإعدادات الافتراضية')
        reset.clicked.connect(self.reset_defaults)
        buttons.addWidget(reset)
        close = QPushButton('إغلاق')
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)

    def _save(self, category):
        value = self.fields[category].text().strip()
        if not value:
            self.status.setText('المسار فارغ؛ لم يُحفظ.')
            return
        try:
            saved = set_storage_path(category, value)
        except (OSError, ValueError) as exc:
            self.fields[category].setText(str(get_storage_path(category)))
            self.status.setText(f'لم يُحفظ المسار: {exc}')
            QMessageBox.warning(self, 'تعذر حفظ مسار التخزين', str(exc))
            return
        self.fields[category].setText(str(saved))
        if category == 'root':
            settings = _settings()
            for other, field in self.fields.items():
                if other != 'root' and not settings.contains(f'storage/{other}'):
                    field.setText(str(default_storage_path(other)))
        self.status.setText(f'حُفظ مسار «{STORAGE_LABELS[category]}».')

    def _browse(self, category):
        current = self.fields[category].text()
        path = QFileDialog.getExistingDirectory(self, f'اختيار: {STORAGE_LABELS[category]}', current)
        if path:
            self.fields[category].setText(path)
            self._save(category)

    def _open(self, category):
        path = Path(self.fields[category].text()).expanduser()
        try:
            validate_storage_path(path, create=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.warning(self, 'تعذر فتح المجلد', str(exc))

    def reset_defaults(self):
        if QMessageBox.question(self, 'استعادة الإعدادات', 'إعادة جميع المسارات إلى القيم الافتراضية؟',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        reset_storage_paths()
        for key, edit in self.fields.items():
            edit.setText(str(default_storage_path(key)))
        self.status.setText('استُعيدت المسارات الافتراضية.')
