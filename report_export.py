"""Editable section ordering and dependency-free PDF/DOCX report export."""
from __future__ import annotations

import html
import json
import os
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PySide6.QtCore import QMarginsF, QBuffer, QIODevice, QUrl
from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QFont, QTextDocument, QTextFormat
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QTextBrowser, QVBoxLayout, QFileDialog

from storage_manager import atomic_write, confirm_overwrite, get_storage_path, validate_storage_path


SECTIONS = [
    ('overview', 'ملخص المشروع'), ('images', 'الصور والمصادر'),
    ('alignment', 'المواءمة'), ('comparison', 'المقارنة'),
    ('change', 'نتائج التغيير'), ('targets', 'الأهداف'), ('objects', 'مجموعات التغيير'),
]


def _json_value(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value if value is not None else '—')


def _image_name(info):
    return info.get('name') or Path(info.get('path') or '').name or '—'


def _target_report_value(target, key, metadata):
    if key in ('target_id', 'id'):
        return target.get('id', target.get('object_id', ''))
    value = target.get('excel_values', {}).get(key, target.get(key, ''))
    return metadata.get(key, value) if value in ('', None) else value


def report_html(snapshot, ordered_keys):
    snap = snapshot or {}
    sections = dict(SECTIONS)
    out = ['<!doctype html><html dir="rtl"><meta charset="utf-8"><style>'
           'body{font-family:Arial,sans-serif;direction:rtl;color:#17202a;margin:28px}'
           'h1{color:#174a72}h2{background:#e9f1f8;padding:8px}table{width:100%;border-collapse:collapse}'
           'td,th{border:1px solid #aab7c4;padding:6px;text-align:right;vertical-align:top}'
           'th{background:#dce8f2}pre{white-space:pre-wrap;word-break:break-word}'
           '</style><body><h1>تقرير ChangeVision 2026</h1>']
    for key in ordered_keys:
        if key not in sections:
            continue
        out.append(f'<h2>{html.escape(sections[key])}</h2>')
        if key == 'overview':
            summary = snap.get('change_summary') or {}
            operation = snap.get('report_metadata') or {}
            rows = [('الصورة قبل', _image_name(snap.get('image1') or {})),
                    ('الصورة بعد', _image_name(snap.get('image2') or {})),
                    ('اسم العملية', operation.get('operation_name', '')),
                    ('العنوان', operation.get('title', '')),
                    ('تاريخ بدء العملية', operation.get('operation_start', '')),
                    ('تاريخ انتهاء العملية', operation.get('operation_end', '')),
                    ('تاريخ الصورة قبل', operation.get('before_date', '')),
                    ('تاريخ الصورة بعد', operation.get('after_date', '')),
                    ('الموقع', operation.get('location', '')),
                    ('Longitude', operation.get('longitude', '')),
                    ('Latitude', operation.get('latitude', '')),
                    ('موقع الصورة قبل', operation.get('before_location', '')),
                    ('موقع الصورة بعد', operation.get('after_location', '')),
                    ('عدد الأهداف', sum(1 for t in snap.get('regions', []) if t.get('report_included', False))),
                    ('مجموعات التغيير', len(snap.get('objects') or []))]
            source_labels = {
                'operation_name': 'اسم العملية', 'title': 'العنوان',
                'operation_start': 'تاريخ بدء العملية', 'operation_end': 'تاريخ انتهاء العملية',
                'before_date': 'تاريخ الصورة قبل', 'after_date': 'تاريخ الصورة بعد',
                'location': 'الموقع', 'longitude': 'Longitude', 'latitude': 'Latitude',
                'before_location': 'موقع الصورة قبل', 'after_location': 'موقع الصورة بعد',
            }
            sources = operation.get('_sources') or {}
            rows += [(f'مصدر {label}', sources.get(field, 'غير محدد'))
                     for field, label in source_labels.items() if operation.get(field) not in ('', None)]
            for role, label in (('before', 'الصورة قبل'), ('after', 'الصورة بعد')):
                info = (operation.get('_image_sources') or {}).get(role) or {}
                if info.get('source'):
                    rows.append((f'مصدر موقع {label}', info.get('source')))
            rows += list(summary.items())
            out.append(_table(rows))
        elif key == 'images':
            rows = []
            for label, image in [('قبل', snap.get('image1') or {}), ('بعد', snap.get('image2') or {})]:
                rows.extend([(f'الصورة {label}', _image_name(image)),
                             (f'المسار {label}', image.get('path', '—')),
                             (f'الأبعاد {label}', f"{image.get('width','—')} × {image.get('height','—')}"),
                             (f'إسناد {label}', image.get('crs', '—'))])
            out.append(_table(rows))
            report_image_items = []
            for target_index, target in enumerate(snap.get('regions') or [], 1):
                if target.get('report_included', False):
                    for item in target.get('report_images') or []:
                        report_image_items.append((int(item.get('report_order', 0)), target_index, target, item))
            for image_index, (_order, target_index, target, item) in enumerate(
                    sorted(report_image_items, key=lambda entry: (entry[0], entry[1])), 1):
                path = str(item.get('path') or '')
                image_path = Path(path)
                image_url = image_path.resolve().as_uri() if image_path.is_file() else ''
                img = f'<br><img src=\"{html.escape(image_url)}\" width=\"520\">' if image_url else ''
                source = item.get('source_label') or item.get('source') or 'Unspecified source'
                target_id = target.get('id', target.get('target_uid', target_index))
                out.append(f'<p><b>Target {html.escape(str(target_id))} | Image {image_index}: {html.escape(str(source))}</b>{img}</p>')
            for index, target in enumerate(snap.get('regions') or [], 1):
                if not target.get('report_included', False):
                    continue
                for role, caption in [('before', 'صورة الهدف قبل'), ('after', 'صورة الهدف بعد')]:
                    state = (target.get('image_layers') or {}).get(role) or {}
                    path = state.get('composite_path') or target.get(f'{role}_image') or target.get(f'{role}_edited_image')
                    if path:
                        image_path = Path(path)
                        image_url = image_path.resolve().as_uri() if image_path.is_file() else ''
                        img = f'<br><img src="{html.escape(image_url)}" width="420">' if image_url else ''
                        out.append(f'<p><b>{caption} {index} — {html.escape(str(target.get("title", target.get("name", ""))))}</b><br>{html.escape(str(path))}{img}</p>')
        elif key in ('alignment', 'comparison', 'change'):
            field = {'alignment': 'alignment', 'comparison': 'comparison', 'change': 'change_summary'}[key]
            out.append(_table((snap.get(field) or {}).items()))
        elif key == 'targets':
            targets = [t for t in (snap.get('regions') or []) if t.get('report_included', False)]
            columns = [c for c in (snap.get('region_columns') or []) if c.get('visible', True)]
            if not columns:
                columns = [{'key': 'id', 'title': 'ID'},
                           {'key': 'target_name', 'title': 'اسم الهدف أو المعلم المحدد'},
                           {'key': 'title', 'title': 'العنوان'},
                           {'key': 'location', 'title': 'الموقع'}, {'key': 'notes', 'title': 'ملاحظات'}]
            out.append(_table([[c.get('title', c['key']) for c in columns]] +
                              [[_target_report_value(t, c['key'], snap.get('report_metadata') or {}) for c in columns] for t in targets], matrix=True))
            for index, target in enumerate(targets, 1):
                rows = [('ID', target.get('id', target.get('target_uid', index))),
                        ('اسم الهدف أو المعلم المحدد', target.get('target_name', '')),
                        ('التصنيف', target.get('target_state', target.get('classification', ''))),
                        ('الموقع', target.get('location', '')),
                        ('Longitude', target.get('longitude', '')),
                        ('Latitude', target.get('latitude', '')),
                        ('الوصف', target.get('description', '')),
                        ('الملاحظات', target.get('notes', '')),
                        ('تاريخ الصورة قبل', target.get('before_date', '')),
                        ('تاريخ الصورة بعد', target.get('after_date', ''))]
                out.append(f'<h3>بيانات الهدف {html.escape(str(target.get("id", index)))}</h3>')
                out.append(_table(rows))
        elif key == 'objects':
            objects = snap.get('objects') or []
            fields = [('object_id','المعرف'), ('change_type','نوع التغيير'), ('type_confidence','الثقة'),
                      ('area_pixels','المساحة بالبكسل'), ('verification_note','ملاحظة التحقق')]
            out.append(_table([[label for _, label in fields]] + [[o.get(k, '') for k, _ in fields] for o in objects], matrix=True))
    out.append('</body></html>')
    return ''.join(out)


def _table(rows, matrix=False):
    if matrix:
        if not rows:
            rows = [['لا توجد بيانات']]
        head, body = rows[0], rows[1:]
        return '<table><thead><tr>' + ''.join(f'<th>{html.escape(_json_value(v))}</th>' for v in head) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{html.escape(_json_value(v))}</td>' for v in row) + '</tr>' for row in body) + '</tbody></table>'
    return '<table>' + ''.join('<tr><th>' + html.escape(str(k)) + '</th><td>' + html.escape(_json_value(v)) + '</td></tr>' for k, v in rows) + '</table>'


def legacy_export_pdf(path, html_doc):
    path = Path(path)
    validate_storage_path(path.parent, create=True)
    def write(temp):
        writer = QPdfWriter(str(temp))
        writer.setPageSize(QPageSize(QPageSize.A4))
        writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Millimeter)
        document = QTextDocument()
        document.setDefaultFont(QFont('Arial', 10))
        document.setBaseUrl(Path.cwd().as_uri() + '/')
        document.setHtml(html_doc)
        document.print_(writer)
    atomic_write(path, write, overwrite=True)


def legacy_export_docx(path, html_doc):
    """Create a valid RTL DOCX package from report HTML using only stdlib."""
    path = Path(path)
    validate_storage_path(path.parent, create=True)
    doc = QTextDocument(); doc.setBaseUrl(Path.cwd().as_uri() + '/'); doc.setHtml(html_doc)
    blocks = []
    media = []
    image_relationships = []
    next_rid = 2
    block = doc.begin()
    while block.isValid():
        runs = []
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid():
                if fragment.charFormat().isImageFormat():
                    image_format = fragment.charFormat().toImageFormat()
                    url = image_format.name()
                    image = doc.resource(QTextDocument.ImageResource, QUrl(url))
                    if hasattr(image, 'toImage'):
                        image = image.toImage()
                    if image is not None and not image.isNull():
                        buffer = QBuffer(); buffer.open(QIODevice.WriteOnly); image.save(buffer, 'PNG')
                        data = bytes(buffer.data()); buffer.close()
                        media_name = f'image{len(media)+1}.png'; media.append((media_name, data))
                        rid = f'rId{next_rid}'; next_rid += 1
                        image_relationships.append((rid, media_name))
                        width_px = image_format.width() or image.width()
                        height_px = image_format.height() or image.height()
                        width_emu = min(5486400, max(100000, int(width_px * 9525)))
                        height_emu = max(100000, int(width_emu * height_px / max(width_px, 1)))
                        runs.append(f'<w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{width_emu}" cy="{height_emu}"/><wp:docPr id="{len(media)}" name="{escape(media_name)}"/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:nvPicPr><pic:cNvPr id="{len(media)}" name="{escape(media_name)}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{width_emu}" cy="{height_emu}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>')
                else:
                    text = fragment.text()
                    if text:
                        runs.append(f'<w:r><w:rPr><w:rtl/></w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>')
            it += 1
        if runs:
            ppr = '<w:pPr><w:bidi/><w:jc w:val="right"/></w:pPr>'
            blocks.append(f'<w:p>{ppr}{"".join(runs)}</w:p>')
        block = block.next()
    body = ''.join(blocks) or '<w:p><w:r><w:t>تقرير ChangeVision 2026</w:t></w:r></w:p>'
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>' + body + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="850" w:right="850" w:bottom="850" w:left="850"/></w:sectPr></w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>'
                     '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                     '</Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>' +
            '</Relationships>')
    rels_doc = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
                ''.join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name}"/>' for rid, name in image_relationships) +
                '</Relationships>')
    if media:
        content_types = content_types.replace('</Types>', '<Default Extension="png" ContentType="image/png"/></Types>')
    def write(temp):
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', content_types)
            z.writestr('_rels/.rels', rels)
            z.writestr('word/document.xml', document)
            if media:
                z.writestr('word/_rels/document.xml.rels', rels_doc)
                for name, data in media:
                    z.writestr('word/media/' + name, data)
    atomic_write(path, write, overwrite=True)


class LegacyReportBuilderDialog(QDialog):
    def __init__(self, snapshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle('إعداد التقرير وتصديره')
        self.resize(1000, 680)
        self.snapshot = snapshot
        layout = QHBoxLayout(self)
        side = QVBoxLayout(); layout.addLayout(side, 1)
        side.addWidget(QLabel('رتب أقسام التقرير بالسحب، أو استخدم الأزرار:'))
        self.list = QListWidget(); self.list.setDragDropMode(QListWidget.InternalMove); side.addWidget(self.list)
        for key, label in SECTIONS:
            item = QListWidgetItem(label); item.setData(256, key); self.list.addItem(item)
        buttons = QHBoxLayout(); side.addLayout(buttons)
        for label, delta in [('أعلى', -1), ('أسفل', 1)]:
            button = QPushButton(label); button.clicked.connect(lambda _=False, d=delta: self._move(d)); buttons.addWidget(button)
        side.addWidget(QLabel('استبعاد قسم بالنقر المزدوج عليه.'))
        export_row = QHBoxLayout(); side.addLayout(export_row)
        pdf = QPushButton('حفظ PDF'); docx = QPushButton('حفظ Word')
        pdf.clicked.connect(lambda: self._export('pdf')); docx.clicked.connect(lambda: self._export('docx'))
        export_row.addWidget(pdf); export_row.addWidget(docx)
        self.list.itemDoubleClicked.connect(self._toggle)
        self.preview = QTextBrowser(); self.preview.setOpenExternalLinks(False); layout.addWidget(self.preview, 2)
        self.list.model().rowsMoved.connect(self.refresh_preview)
        self.refresh_preview()

    def _keys(self):
        return [self.list.item(i).data(256) for i in range(self.list.count())]

    def _toggle(self, item):
        item.setHidden(not item.isHidden()); self.refresh_preview()

    def _move(self, delta):
        row = self.list.currentRow(); dest = row + delta
        if row < 0 or dest < 0 or dest >= self.list.count(): return
        item = self.list.takeItem(row); self.list.insertItem(dest, item); self.list.setCurrentRow(dest); self.refresh_preview()

    def refresh_preview(self, *args):
        keys = [self.list.item(i).data(256) for i in range(self.list.count()) if not self.list.item(i).isHidden()]
        self.current_html = report_html(self.snapshot, keys)
        self.preview.setHtml(self.current_html)

    def _export(self, kind):
        folder = get_storage_path('pdf' if kind == 'pdf' else 'word')
        default = folder / ('ChangeVision_Report.' + ('pdf' if kind == 'pdf' else 'docx'))
        path, _ = QFileDialog.getSaveFileName(self, 'حفظ التقرير', str(default), 'PDF (*.pdf)' if kind == 'pdf' else 'Word (*.docx)')
        if not path: return
        if not path.lower().endswith('.' + kind): path += '.' + kind
        try:
            validate_storage_path(Path(path).parent, create=True)
            if not confirm_overwrite(self, path): return
            (export_pdf if kind == 'pdf' else export_docx)(path, self.current_html)
            QMessageBox.information(self, 'تم الحفظ', 'تم إنشاء التقرير بنجاح:\n' + path)
        except Exception as exc:
            QMessageBox.critical(self, 'تعذر إنشاء التقرير', str(exc))


# Keep the existing import/call signature in main.py. Analysis, CVP, targets and
# automatic TXT/XLSX paths do not change. These legacy export signatures remain
# available for existing callers, using the new native-table backends.
def export_docx(path, html_doc):
    from report_adapter import from_legacy_html
    from report_docx import export_docx as write_docx
    document, assets = from_legacy_html(html_doc)
    return write_docx(path, document, assets, overwrite=True)


def export_pdf(path, html_doc):
    from report_adapter import from_legacy_html
    from report_layout import export_pdf as write_pdf
    document, assets = from_legacy_html(html_doc)
    return write_pdf(path, document, assets, overwrite=True)


from report_editor import ReportEditorDialog as ReportBuilderDialog
