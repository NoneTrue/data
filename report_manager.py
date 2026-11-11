from __future__ import annotations

import os
import json
import datetime
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from storage_manager import atomic_write, validate_storage_path


_INVALID_SHEET = set('[]:*?/\\')


def _safe_sheet_name(name: str, used: set[str]) -> str:
    base = ''.join('_' if c in _INVALID_SHEET else c for c in str(name)).strip() or 'Sheet'
    base = base[:31]
    out = base
    n = 2
    while out in used:
        suffix = f'_{n}'
        out = base[:31-len(suffix)] + suffix
        n += 1
    used.add(out)
    return out


def _col_letter(n: int) -> str:
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_text(v) -> str:
    if v is None:
        return ''
    if isinstance(v, (dict, list, tuple)):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    return escape(str(v))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _make_sheet_xml(rows, rtl=True):
    rows = list(rows or [])
    max_cols = max([len(r) for r in rows] + [1])
    widths = [10] * max_cols
    for r in rows[:1000]:
        for i, v in enumerate(r):
            txt = str(v if v is not None else '')
            widths[i] = min(42, max(widths[i], min(42, len(txt) + 2)))

    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    out.append('<sheetViews><sheetView workbookViewId="0" rightToLeft="%s">'
               '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
               '</sheetView></sheetViews>' % ('1' if rtl else '0'))
    out.append('<sheetFormatPr defaultRowHeight="18"/>')
    out.append('<cols>')
    for i, w in enumerate(widths, start=1):
        out.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
    out.append('</cols><sheetData>')

    for ri, row in enumerate(rows, start=1):
        out.append(f'<row r="{ri}" ht="{22 if ri == 1 else 18}" customHeight="1">')
        for ci, v in enumerate(row, start=1):
            ref = f'{_col_letter(ci)}{ri}'
            style = 1 if ri == 1 else 2
            if _is_number(v):
                out.append(f'<c r="{ref}" s="{3 if ri > 1 else 1}" t="n"><v>{v}</v></c>')
            else:
                txt = _xml_text(v)
                out.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{txt}</t></is></c>')
        out.append('</row>')
    out.append('</sheetData>')
    if rows and len(rows[0]) > 1 and len(rows) > 1:
        end = f'{_col_letter(len(rows[0]))}{len(rows)}'
        out.append(f'<autoFilter ref="A1:{end}"/>')
    out.append('</worksheet>')
    return ''.join(out)


def write_xlsx(path: str | os.PathLike, sheets):
    """Write a compact formatted XLSX using only Python stdlib."""
    path = str(path)
    validate_storage_path(Path(path).parent, create=True)
    used = set()
    sheet_defs = []
    for name, rows in sheets:
        sheet_defs.append((_safe_sheet_name(name, used), list(rows or [])))

    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(1, len(sheet_defs)+1):
        ct.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    ct.append('</Types>')

    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                 '</Relationships>')

    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>']
    for i, (name, _) in enumerate(sheet_defs, start=1):
        wb.append(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>')
    wb.append('</sheets></workbook>')

    wb_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(1, len(sheet_defs)+1):
        wb_rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
    wb_rels.append(f'<Relationship Id="rId{len(sheet_defs)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    wb_rels.append('</Relationships>')

    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="10"/><name val="Segoe UI"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Segoe UI"/></font>
  </fonts>
  <fills count="3">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border><left style="thin"><color rgb="FFD9E2F3"/></left><right style="thin"><color rgb="FFD9E2F3"/></right><top style="thin"><color rgb="FFD9E2F3"/></top><bottom style="thin"><color rgb="FFD9E2F3"/></bottom><diagonal/></border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="4">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="center" wrapText="1"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    tmp = path + '.tmp'
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', ''.join(ct))
        z.writestr('_rels/.rels', root_rels)
        z.writestr('xl/workbook.xml', ''.join(wb))
        z.writestr('xl/_rels/workbook.xml.rels', ''.join(wb_rels))
        z.writestr('xl/styles.xml', styles)
        for i, (_, rows) in enumerate(sheet_defs, start=1):
            z.writestr(f'xl/worksheets/sheet{i}.xml', _make_sheet_xml(rows, rtl=True))
    os.replace(tmp, path)


class ReportManager:
    """Automatic session reporting: text + Excel after every major operation."""

    def __init__(self, project_dir: str, app_title: str, report_group: str | None = None,
                 report_root: str | os.PathLike | None = None,
                 excel_root: str | os.PathLike | None = None):
        now = datetime.datetime.now()
        self.app_title = app_title
        self.started = now
        self.project_dir = Path(project_dir)
        self.root = Path(report_root) if report_root else self.project_dir / 'Reports'
        if report_group:
            self.root = self.root / str(report_group)
        validate_storage_path(self.root, create=True)
        self.day_dir = self.root / now.strftime('%Y-%m-%d')
        self.session_dir = self.day_dir / ('Session_' + now.strftime('%Y%m%d_%H%M%S'))
        self.operations_dir = self.session_dir / 'Operations'
        self.operations_dir.mkdir(parents=True, exist_ok=True)
        self.txt_path = self.session_dir / 'Project_Report.txt'
        self.log_path = self.session_dir / 'Operations_Log.txt'
        self.excel_session_dir = (Path(excel_root) / now.strftime('%Y-%m-%d') /
                                  self.session_dir.name) if excel_root else self.session_dir
        validate_storage_path(self.excel_session_dir, create=True)
        self.xlsx_path = self.excel_session_dir / 'Project_Results.xlsx'
        self.latest_path = self.root / 'LATEST_REPORT_PATH.txt'
        self.operations = []
        self.last_snapshot = {}
        self.counter = 0
        atomic_write(self.latest_path, lambda temp: temp.write_text(str(self.session_dir), encoding='utf-8'), overwrite=True)
        self.record('بدء جلسة جديدة', 'نجاح', 'تم إنشاء مجلد التقارير تلقائيًا.', {})

    def record(self, operation: str, status: str='نجاح', details: str='', snapshot=None):
        self.counter += 1
        now = datetime.datetime.now()
        snap = snapshot or self.last_snapshot or {}
        self.last_snapshot = snap
        row = {
            'رقم': self.counter,
            'الوقت': now.strftime('%Y-%m-%d %H:%M:%S'),
            'العملية': str(operation),
            'الحالة': str(status),
            'التفاصيل': str(details or ''),
        }
        self.operations.append(row)
        with self.log_path.open('a', encoding='utf-8') as f:
            f.write(f"[{row['الوقت']}] #{row['رقم']} | {row['العملية']} | {row['الحالة']} | {row['التفاصيل']}\n")
        op_name = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in str(operation)).strip().replace(' ', '_')[:50]
        op_file = self.operations_dir / f"{self.counter:03d}_{now.strftime('%H%M%S')}_{op_name}.txt"
        atomic_write(op_file, lambda temp: temp.write_text(self._operation_text(row, snap), encoding='utf-8'))
        self._write_summary_txt(snap)
        self._write_excel(snap)
        return str(op_file)


    @staticmethod
    def _normalized_alignment(snap):
        """Return alignment data including precise SIFT/RANSAC fields when available."""
        alignment = dict((snap.get('alignment') or {}))
        precise = snap.get('precise_alignment') or {}
        if isinstance(precise, dict):
            # Preserve existing alignment keys and only add missing precise fields.
            mapping = [
                ('sift_before_keypoints', 'SIFT BEFORE'),
                ('sift_after_keypoints', 'SIFT AFTER'),
                ('good_matches', 'Good Matches'),
                ('inliers', 'Inliers'),
                ('inlier_ratio_percent', 'Inlier Ratio %'),
                ('x_shift_px', 'X Shift (px)'),
                ('y_shift_px', 'Y Shift (px)'),
                ('total_shift_px', 'Total Shift (px)'),
                ('rotation_deg', 'Rotation (deg)'),
                ('scale', 'Scale'),
                ('before_x_shift_px', 'Before X Shift (px)'),
                ('before_y_shift_px', 'Before Y Shift (px)'),
                ('before_total_shift_px', 'Before Total Shift (px)'),
                ('after_x_shift_px', 'After X Shift (px)'),
                ('after_y_shift_px', 'After Y Shift (px)'),
                ('after_total_shift_px', 'After Total Shift (px)'),
                ('before_inlier_ratio_percent', 'Before Inlier Ratio %'),
                ('after_inlier_ratio_percent', 'After Inlier Ratio %'),
                ('result', 'Result'),
            ]
            for src_key, label in mapping:
                if src_key in precise and label not in alignment:
                    alignment[label] = precise[src_key]
            # Also keep any additional precise fields not covered above.
            for k, v in precise.items():
                if k not in alignment and k not in {m[0] for m in mapping}:
                    alignment[k] = v
        return alignment

    @staticmethod
    def _normalized_change_summary(snap):
        """Return change summary with classification counts completed from objects when possible."""
        change = dict((snap.get('change_summary') or {}))
        objects = snap.get('objects') or []

        counts = {
            'appearance_count': 0,
            'disappearance_count': 0,
            'structural_count': 0,
            'radiometric_count': 0,
            'stable_count': 0,
        }
        found_classification = False

        for o in objects:
            c = str(o.get('change_type', o.get('classification', '')) or '').strip().lower()
            if not c:
                continue
            found_classification = True
            if c in ('appearance', 'appeared', 'new', 'ظهور', 'ظهر', 'جديد'):
                counts['appearance_count'] += 1
            elif c in ('disappearance', 'disappeared', 'removed', 'اختفاء', 'اختفى', 'إزالة', 'ازالة'):
                counts['disappearance_count'] += 1
            elif c in ('structural', 'structure', 'structural_change', 'تغير بنيوي', 'هيكلي', 'بنيوي'):
                counts['structural_count'] += 1
            elif c in ('radiometric', 'radiometric_change', 'brightness', 'تغير إشعاعي', 'اشعاعي', 'إشعاعي'):
                counts['radiometric_count'] += 1
            elif c in ('stable', 'unchanged', 'no_change', 'ثابت', 'بدون تغيير'):
                counts['stable_count'] += 1

        if found_classification:
            for k, v in counts.items():
                # Only fill absent/empty values; do not overwrite real values produced by the engine.
                if k not in change or change.get(k) in (None, ''):
                    change[k] = v

        # Accept a dedicated classification_summary dict when the app provides one.
        cls = snap.get('classification_summary') or {}
        if isinstance(cls, dict):
            for k, v in cls.items():
                change[k] = v

        return change

    def _operation_text(self, row, snap):
        lines = [
            'ChangeVision 2026 - تقرير عملية',
            '=' * 70,
            f"رقم العملية: {row['رقم']}",
            f"الوقت: {row['الوقت']}",
            f"العملية: {row['العملية']}",
            f"الحالة: {row['الحالة']}",
            f"التفاصيل: {row['التفاصيل']}",
            '',
        ]
        lines.extend(self._summary_lines(snap))
        return '\n'.join(lines) + '\n'

    def _summary_lines(self, snap):
        out = ['ملخص الحالة الحالية', '-' * 70]
        for key in ('image1','image2'):
            d = snap.get(key) or {}
            out.append(f"{key}: {d.get('path','—')}")
            if d:
                out.append(f"  الحجم: {d.get('width','—')} × {d.get('height','—')} | CRS: {d.get('crs','—')} | Pixel Size: {d.get('pixel_size','—')}")
        settings = snap.get('settings') or {}
        if settings:
            out += ['', 'الإعدادات:'] + [f"  {k}: {v}" for k,v in settings.items()]
        alignment = self._normalized_alignment(snap)
        if alignment:
            out += ['', 'المواءمة:'] + [f"  {k}: {v}" for k,v in alignment.items()]
        comparison = snap.get('comparison') or {}
        if comparison:
            out += ['', 'المقارنة:'] + [f"  {k}: {v}" for k,v in comparison.items()]
        change = self._normalized_change_summary(snap)
        if change:
            out += ['', 'نتائج التغيير:'] + [f"  {k}: {v}" for k,v in change.items()]
        out.append('')
        return out

    def _write_summary_txt(self, snap):
        lines = [
            self.app_title,
            'تقرير نتائج المشروع - حفظ تلقائي بعد كل عملية',
            '=' * 88,
            f"بداية الجلسة: {self.started.strftime('%Y-%m-%d %H:%M:%S')}",
            f"آخر تحديث: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"مجلد الجلسة: {self.session_dir}",
            '',
        ]
        lines.extend(self._summary_lines(snap))
        lines += ['سجل العمليات', '-' * 88]
        for r in self.operations:
            lines.append(f"#{r['رقم']:03d} | {r['الوقت']} | {r['العملية']} | {r['الحالة']} | {r['التفاصيل']}")
        atomic_write(self.txt_path, lambda temp: temp.write_text('\n'.join(lines) + '\n', encoding='utf-8'), overwrite=True)

    @staticmethod
    def _kv_rows(title, d):
        rows = [['البند', 'القيمة']]
        for k, v in (d or {}).items():
            if isinstance(v, (dict, list, tuple)):
                v = json.dumps(v, ensure_ascii=False)
            rows.append([k, v])
        if len(rows) == 1:
            rows.append(['الحالة', 'لا توجد بيانات بعد'])
        return rows

    def _write_excel(self, snap):
        summary = [['البند', 'القيمة'],
                   ['اسم البرنامج', self.app_title],
                   ['بداية الجلسة', self.started.strftime('%Y-%m-%d %H:%M:%S')],
                   ['آخر تحديث', datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')],
                   ['مجلد الجلسة', str(self.session_dir)]]
        for key, label in [('image1','الصورة قبل'),('image2','الصورة بعد')]:
            d = snap.get(key) or {}
            summary += [[label + ' - المسار', d.get('path','—')],
                        [label + ' - الحجم', f"{d.get('width','—')} × {d.get('height','—')}"],
                        [label + ' - CRS', d.get('crs','—')],
                        [label + ' - Pixel Size', d.get('pixel_size','—')]]
        cs = self._normalized_change_summary(snap)
        for k in ('similarity_percent','changed_percent','object_count','accepted_count','review_count','rejected_count','changed_pixels','changed_area_map_units2','changed_area_m2','polygon_total_area_m2','appearance_count','disappearance_count','structural_count','radiometric_count','stable_count','verification_review_count','verified_change_count','candidate_changed_percent'):
            if k in cs:
                summary.append([k, cs[k]])

        ops = [['#','الوقت','العملية','الحالة','التفاصيل']]
        for r in self.operations:
            ops.append([r['رقم'],r['الوقت'],r['العملية'],r['الحالة'],r['التفاصيل']])

        regions = list(snap.get('regions') or [])
        default_columns = [
            ('navigate','انتقال'), ('target_id','ID'), ('operation_name','اسم العملية'),
            ('target_name','اسم الهدف أو المعلم المحدد'), ('title','العنوان'), ('operation_start','تاريخ بدء العملية'),
            ('operation_end','تاريخ انتهاء العملية'), ('before_date','تاريخ الصورة قبل'),
            ('after_date','تاريخ الصورة بعد'), ('location','الموقع'),
            ('longitude','Longitude'), ('latitude','Latitude'), ('notes','الملاحظات'),
        ]
        columns = [c for c in (snap.get('region_columns') or []) if c.get('visible', True)]
        if not columns:
            columns = [{'key': key, 'title': title} for key, title in default_columns]
        reg_rows = [[c.get('title', c.get('key', '')) for c in columns]]
        included = [r for r in regions if r.get('report_included', False)]
        for idx, r in enumerate(included, start=1):
            values = []
            for column in columns:
                key = column.get('key')
                if key == 'navigate':
                    value = 'انتقال'
                elif key == 'target_id':
                    value = r.get('id', r.get('object_id', idx - 1))
                elif key in ('longitude', 'latitude'):
                    value = r.get(key, '') if r.get(key) is not None else ''
                else:
                    value = r.get('excel_values', {}).get(key, r.get(key, ''))
                    if value in ('', None):
                        value = (snap.get('report_metadata') or {}).get(key, value)
                values.append(value)
            reg_rows.append(values)
        if not included:
            reg_rows.append(['لا توجد أهداف مختارة للتقرير'] + [''] * (len(columns) - 1))

        objects = snap.get('objects') or []
        obj_headers = ['object_id','change_type','type_confidence','is_real_change','before_presence_pct','after_presence_pct','cross_similarity_pct','edge_overlap_pct','object_correspondence_pct','object_edge_overlap_pct','object_before_strength_pct','object_after_strength_pct','object_shift_px','shadow_likelihood_pct','verification_note','parts','area_pixels','edge_delta','brightness_delta','cx','cy','x','y','width','height']
        obj_rows = [obj_headers]
        for o in objects:
            edge_delta = float(o.get('edge_after',0) or 0) - float(o.get('edge_before',0) or 0)
            brightness_delta = float(o.get('signed_mean',0) or 0)
            obj_rows.append([
                o.get('object_id',''), o.get('change_type',''), o.get('type_confidence',o.get('confidence','')), o.get('is_real_change',False),
                o.get('before_presence_pct',''), o.get('after_presence_pct',''), o.get('cross_similarity_pct',''), o.get('edge_overlap_pct',''),
                o.get('object_correspondence_pct',''), o.get('object_edge_overlap_pct',''), o.get('object_before_strength_pct',''), o.get('object_after_strength_pct',''),
                o.get('object_shift_px',''), o.get('shadow_likelihood_pct',''), o.get('verification_note',''),
                o.get('parts',''), o.get('area_pixels',''), edge_delta, brightness_delta, o.get('cx',''), o.get('cy',''), o.get('x',''), o.get('y',''), o.get('width',''), o.get('height','')
            ])
        if len(obj_rows)==1: obj_rows.append(['لا توجد مجموعات بعد'] + ['']*(len(obj_headers)-1))

        sheets = [
            ('الملخص', summary),
            ('العمليات', ops),
            ('الإعدادات', self._kv_rows('settings', snap.get('settings') or {})),
            ('المواءمة', self._kv_rows('alignment', self._normalized_alignment(snap))),
            ('المقارنة', self._kv_rows('comparison', snap.get('comparison') or {})),
            ('نتائج التغيير', self._kv_rows('change', self._normalized_change_summary(snap))),
            ('المناطق', reg_rows),
            ('مجموعات التغيير', obj_rows),
        ]
        write_xlsx(self.xlsx_path, sheets)

    def open_folder(self):
        p = str(self.session_dir)
        if os.name == 'nt':
            os.startfile(p)
        else:
            import subprocess
            subprocess.Popen(['xdg-open', p])
        return p
