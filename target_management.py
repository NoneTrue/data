"""Editable target metadata and report-column tools for ChangeVision projects."""
from __future__ import annotations

import copy
import uuid

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGridLayout, QHBoxLayout,
    QComboBox, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QPixmap
from image_editor import ImageEditorDialog


TARGET_STATES = ('مهم', 'حساس', 'تغير كبير', 'عادي', 'مستبعد')

DEFAULT_COLUMNS = [
    {'key': 'navigate', 'title': 'انتقال', 'visible': True, 'fixed': True},
    {'key': 'target_id', 'title': 'ID', 'visible': True, 'fixed': True},
    {'key': 'operation_name', 'title': 'اسم العملية', 'visible': True},
    {'key': 'target_name', 'title': 'اسم الهدف أو المعلم المحدد', 'visible': True},
    {'key': 'title', 'title': 'العنوان', 'visible': True},
    {'key': 'operation_start', 'title': 'تاريخ بدء العملية', 'visible': True},
    {'key': 'operation_end', 'title': 'تاريخ انتهاء العملية', 'visible': True},
    {'key': 'before_date', 'title': 'تاريخ الصورة قبل', 'visible': True},
    {'key': 'after_date', 'title': 'تاريخ الصورة بعد', 'visible': True},
    {'key': 'location', 'title': 'الموقع', 'visible': True},
    {'key': 'longitude', 'title': 'Longitude', 'visible': True},
    {'key': 'latitude', 'title': 'Latitude', 'visible': True},
    {'key': 'notes', 'title': 'الملاحظات', 'visible': True},
]


def ensure_target_metadata(regions):
    """Add target metadata without removing or rewriting analytical fields."""
    for index, region in enumerate(regions or [], 1):
        region.setdefault('target_uid', str(uuid.uuid4()))
        # Target names are supplied by the user; never synthesize a label.
        region.setdefault('target_name', '')
        region.setdefault('description', '')
        region.setdefault('notes', '')
        region.setdefault('target_state', region.get('classification', 'عادي'))
        # Reports include only targets the user explicitly selected.
        region.setdefault('report_included', False)
        region.setdefault('before_image', '')
        region.setdefault('after_image', '')
        region.setdefault('excel_values', {})
        for key in ('operation_name', 'title', 'operation_start', 'operation_end',
                    'before_date', 'after_date', 'location'):
            region.setdefault(key, '')
    return regions


def visible_columns(columns=None):
    result = copy.deepcopy(columns if columns else DEFAULT_COLUMNS)
    if result and not any(column.get('key') == 'target_name' for column in result):
        position = next((i + 1 for i, column in enumerate(result)
                         if column.get('key') == 'operation_name'), min(3, len(result)))
        result.insert(position, copy.deepcopy(next(c for c in DEFAULT_COLUMNS if c['key'] == 'target_name')))
    return result


def value_for_column(region, column):
    key = column.get('key')
    if key == 'navigate':
        return 'انتقال'
    if key == 'target_id':
        return region.get('id', region.get('object_id', ''))
    if key in ('longitude', 'latitude'):
        return region.get(key, '') if region.get(key) is not None else ''
    return region.get('excel_values', {}).get(key, region.get(key, ''))


def set_column_value(region, column, value):
    key = column.get('key')
    if key in ('navigate', 'target_id'):
        return
    if key in ('longitude', 'latitude'):
        try:
            region[key] = float(value) if str(value).strip() else None
        except (TypeError, ValueError):
            region[key] = str(value)
    elif key == 'target_name':
        region['target_name'] = str(value)
    elif key in ('operation_name', 'title', 'operation_start', 'operation_end',
                 'before_date', 'after_date', 'location', 'notes'):
        region[key] = str(value)
    else:
        region.setdefault('excel_values', {})[key] = str(value)


class TargetEditorDialog(QDialog):
    def __init__(self, target, parent=None, manual=False):
        super().__init__(parent)
        self.setWindowTitle('بيانات الهدف')
        self.setMinimumWidth(520)
        self.target = copy.deepcopy(target)
        self.manual = manual
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(str(target.get('target_name', '')))
        self.description = QTextEdit(str(target.get('description', '')))
        self.description.setMaximumHeight(75)
        self.notes = QTextEdit(str(target.get('notes', '')))
        self.notes.setMaximumHeight(75)
        self.state = QComboBox()
        self.state.setEditable(True)
        self.state.addItems(TARGET_STATES)
        current_state = str(target.get('target_state', 'عادي'))
        if self.state.findText(current_state) < 0:
            self.state.addItem(current_state)
        self.state.setCurrentText(current_state)
        self.fields = {}
        labels = [('operation_name', 'اسم العملية'), ('title', 'العنوان'),
                  ('operation_start', 'تاريخ بدء العملية'), ('operation_end', 'تاريخ انتهاء العملية'),
                  ('before_date', 'تاريخ الصورة قبل'), ('after_date', 'تاريخ الصورة بعد'),
                  ('location', 'الموقع')]
        form.addRow('اسم الهدف', self.name)
        form.addRow('الوصف', self.description)
        form.addRow('الملاحظات', self.notes)
        form.addRow('التصنيف', self.state)
        for key, label in labels:
            edit = QLineEdit(str(target.get(key, '')))
            self.fields[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        self.before_path = QLineEdit(str(target.get('before_image', '')))
        self.after_path = QLineEdit(str(target.get('after_image', '')))
        for label, field in (('صورة قبل', self.before_path), ('صورة بعد', self.after_path)):
            row = QHBoxLayout()
            browse = QPushButton('استعراض…')
            browse.clicked.connect(lambda _=False, target_field=field: self._browse_image(target_field))
            row.addWidget(field, 1)
            row.addWidget(browse)
            layout.addWidget(QLabel(label))
            layout.addLayout(row)
        if manual:
            for key, label, default in (('x', 'X', 0), ('y', 'Y', 0), ('width', 'العرض بالبكسل', 1), ('height', 'الارتفاع بالبكسل', 1)):
                edit = QLineEdit(str(target.get(key, default)))
                self.fields[key] = edit
                form.addRow(label, edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse_image(self, field):
        path, _ = QFileDialog.getOpenFileName(
            self, 'اختيار صورة مرتبطة بالهدف', field.text(),
            'Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;All files (*)')
        if path:
            field.setText(path)

    def accept(self):
        self.target.update({
            'target_name': self.name.text().strip(),
            'description': self.description.toPlainText(),
            'notes': self.notes.toPlainText(),
            'target_state': self.state.currentText().strip() or 'عادي',
            'before_image': self.before_path.text().strip(),
            'after_image': self.after_path.text().strip(),
        })
        for key, field in self.fields.items():
            raw = field.text().strip()
            if self.manual and key in ('x', 'y', 'width', 'height'):
                try:
                    self.target[key] = int(raw)
                except ValueError:
                    QMessageBox.warning(self, 'قيمة غير صالحة', f'أدخل رقمًا صحيحًا للحقل {key}.')
                    return
            else:
                self.target[key] = raw
        super().accept()


class TargetManagerDialog(QDialog):
    """Manage target metadata and report inclusion separately from analysis."""
    def __init__(self, regions, parent=None):
        super().__init__(parent)
        self.setWindowTitle('إدارة الأهداف')
        self.resize(1050, 540)
        self.regions = ensure_target_metadata(copy.deepcopy(regions or []))
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['للتقرير', 'ID', 'اسم الهدف', 'التصنيف', 'الوصف', 'صورة قبل', 'صورة بعد'])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.horizontalHeader().setStretchLastSection(True)
        work = QHBoxLayout()
        work.addWidget(self.table, 1)
        previews = QVBoxLayout()
        self.before_preview = QLabel('معاينة صورة قبل')
        self.after_preview = QLabel('معاينة صورة بعد')
        for label in (self.before_preview, self.after_preview):
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumSize(180, 130)
            label.setMaximumWidth(240)
            label.setStyleSheet('background:#111923;border:1px solid #33465b;color:#aab6c5;')
            previews.addWidget(label)
        work.addLayout(previews)
        layout.addLayout(work, 1)
        buttons = QHBoxLayout()
        for text, callback in (
            ('تحديد الكل', self.select_all), ('إلغاء التحديد', self.deselect_all),
            ('إضافة هدف يدوي', self.add_manual), ('تعديل الهدف', self.edit_selected),
            ('اقتراح المهم', lambda: self.suggest_state('مهم')),
            ('اقتراح الحساس', lambda: self.suggest_state('حساس')),
            ('تحرير صورة الهدف', self.edit_target_image),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        footer = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        footer.accepted.connect(self.accept)
        footer.rejected.connect(self.reject)
        layout.addWidget(footer)
        self.table.currentCellChanged.connect(self.update_preview)
        self.refresh()

    def refresh(self):
        self.table.setRowCount(len(self.regions))
        for row, target in enumerate(self.regions):
            included = QTableWidgetItem()
            included.setFlags(included.flags() | Qt.ItemIsUserCheckable)
            included.setCheckState(Qt.Checked if target.get('report_included', False) else Qt.Unchecked)
            self.table.setItem(row, 0, included)
            values = [target.get('id', target.get('target_uid', '')), target.get('target_name', ''),
                      target.get('target_state', 'عادي'), target.get('description', ''),
                      target.get('before_image', ''), target.get('after_image', '')]
            for col, value in enumerate(values, 1):
                self.table.setItem(row, col, QTableWidgetItem(str(value)))
        if self.regions:
            self.update_preview(0, 0, -1, -1)

    def update_preview(self, row, _column, _previous_row, _previous_column):
        if not 0 <= row < len(self.regions):
            return
        target = self.regions[row]
        for label, key, caption in ((self.before_preview, 'before_image', 'صورة قبل'),
                                    (self.after_preview, 'after_image', 'صورة بعد')):
            role='before' if key=='before_image' else 'after'
            path = target.get(f'{role}_edited_image') or target.get(key, '')
            pixmap = QPixmap(path) if path else QPixmap()
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
                label.setToolTip(path)
            else:
                label.setPixmap(QPixmap())
                label.setText(f'{caption}: لا توجد معاينة')

    def select_all(self):
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.Checked)
        self.table.selectAll()

    def deselect_all(self):
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.Unchecked)
        self.table.clearSelection()

    def add_manual(self):
        uid = str(uuid.uuid4())
        target = {'target_uid': uid, 'id': f'MANUAL-{len(self.regions) + 1}', 'object_id': uid,
                  'target_name': '', 'target_state': 'عادي',
                  'classification': 'مضاف يدويًا', 'area_pixels': 0, 'area_map_units2': None,
                  'mean_intensity': 0.0, 'max_intensity': 0.0, 'confidence': 100.0,
                  'x': 0, 'y': 0, 'width': 1, 'height': 1, 'report_included': False,
                  'manual_target': True, 'excel_values': {}}
        editor = TargetEditorDialog(target, self, manual=True)
        if editor.exec() == QDialog.Accepted:
            self.regions.append(editor.target)
            self.refresh()
            self.table.selectRow(len(self.regions) - 1)

    def suggest_state(self, state):
        if not self.regions:
            QMessageBox.information(self, 'اقتراح الأهداف', 'لا توجد أهداف لتقييمها.')
            return
        ranked = sorted(self.regions, key=lambda r: (
            float(r.get('confidence', r.get('confidence_pct', 0)) or 0),
            int(r.get('area_pixels', 0) or 0)), reverse=True)
        count = max(1, (len(ranked) + 4) // 5)
        candidates = ranked[:count]
        names = '، '.join(str(r.get('target_name', r.get('id', ''))) for r in candidates[:8])
        if len(candidates) > 8:
            names += f'، و{len(candidates)-8} أخرى'
        answer = QMessageBox.question(
            self, 'اقتراح تصنيف',
            f'اقتراح تصنيف {len(candidates)} هدفًا كـ «{state}» اعتمادًا على الثقة والمساحة؟\n{names}',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            for target in candidates:
                target['target_state'] = state
            self.refresh()

    def edit_target_image(self):
        rows=sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if len(rows)!=1:
            QMessageBox.information(self,'محرر الصور','حدد هدفًا واحدًا لتحرير صورته.')
            return
        row=rows[0];target=self.regions[row]
        role,ok=QInputDialog.getItem(self,'اختيار صورة الهدف','الصورة المراد تحريرها:',
                                     ['قبل','بعد'],0,False)
        if not ok:return
        key='before' if role=='قبل' else 'after'
        path=target.get(f'{key}_image','')
        if not path or not Path(path).is_file():
            path,_=QFileDialog.getOpenFileName(self,'اختيار صورة مرتبطة بالهدف','',
                'All files (*);;Images (*)')
            if not path:return
            target[f'{key}_image']=path
        state=(target.get('image_layers') or {}).get(key,{})
        dialog=ImageEditorDialog(path,state,self)
        if dialog.exec()==QDialog.Accepted and getattr(dialog,'result',None):
            target.setdefault('image_layers',{})[key]=dialog.result
            target[f'{key}_edited_image']=dialog.result['composite_path']
            self.refresh();self.table.selectRow(row);self.update_preview(row,0,-1,-1)

    def edit_selected(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, 'إدارة الأهداف', 'حدد هدفًا واحدًا على الأقل.')
            return
        if len(rows) > 1:
            QMessageBox.information(self, 'إدارة الأهداف', 'حرر كل هدف على حدة للحفاظ على بياناته المستقلة.')
            return
        row = rows[0]
        editor = TargetEditorDialog(self.regions[row], self, bool(self.regions[row].get('manual_target')))
        if editor.exec() == QDialog.Accepted:
            self.regions[row] = editor.target
            self.refresh()
            self.table.selectRow(row)

    def accept(self):
        for row, target in enumerate(self.regions):
            target['report_included'] = self.table.item(row, 0).checkState() == Qt.Checked
        super().accept()


class SpreadsheetEditorDialog(QDialog):
    """Edit report cells and a stable-key column schema, then return project data."""
    def __init__(self, regions, columns=None, metadata=None, parent=None, image_paths=()):
        super().__init__(parent)
        self.setWindowTitle('محرر Excel وجدول المناطق')
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.resize(1200, 620)
        self.regions = ensure_target_metadata(copy.deepcopy(regions or []))
        self.columns = visible_columns(columns)
        self.metadata = copy.deepcopy(metadata or {})
        self.image_paths = tuple(image_paths or ())
        self.metadata_sources = copy.deepcopy(self.metadata.get('_sources') or {})
        for key in ('operation_name', 'title', 'operation_start', 'operation_end', 'before_date',
                    'after_date', 'location', 'longitude', 'latitude', 'before_location',
                    'after_location'):
            if self.metadata.get(key) not in ('', None) and key not in self.metadata_sources:
                # Legacy values have unknown origin; protect them as user-entered.
                self.metadata_sources[key] = 'user'
        self.image_sources = copy.deepcopy(self.metadata.get('_image_sources') or {})
        root = QVBoxLayout(self)
        meta = QGridLayout()
        self.meta_edits = {}
        metadata_fields = (
            ('operation_name', 'اسم العملية'), ('title', 'العنوان'),
            ('operation_start', 'تاريخ بدء العملية'), ('operation_end', 'تاريخ انتهاء العملية'),
            ('before_date', 'تاريخ الصورة قبل'), ('after_date', 'تاريخ الصورة بعد'),
            ('location', 'الموقع الموحد'), ('longitude', 'Longitude'), ('latitude', 'Latitude'),
            ('before_location', 'موقع الصورة قبل'), ('after_location', 'موقع الصورة بعد'),
        )
        self.metadata_initial = {}
        for index, (key, label) in enumerate(metadata_fields):
            edit = QLineEdit(str(self.metadata.get(key, '')))
            edit.setPlaceholderText(label)
            edit.setToolTip('المصدر: ' + str(self.metadata_sources.get(key, 'غير محدد')))
            self.meta_edits[key] = edit
            self.metadata_initial[key] = edit.text()
            row, column = divmod(index, 4)
            meta.addWidget(QLabel(label), row * 2, column)
            meta.addWidget(edit, row * 2 + 1, column)
        root.addLayout(meta)
        metadata_actions = QHBoxLayout()
        self.metadata_status = QLabel('مصدر البيانات: لم يُجرِ استخراج تلقائي في هذه الجلسة.')
        metadata_actions.addWidget(self.metadata_status, 1)
        self.extract_button = QPushButton('تحديث البيانات تلقائياً')
        self.extract_button.clicked.connect(self.refresh_metadata_from_images)
        metadata_actions.addWidget(self.extract_button)
        self.geocode_button = QPushButton('بحث اختياري عن أسماء الأماكن')
        self.geocode_button.clicked.connect(self.lookup_place_names)
        metadata_actions.addWidget(self.geocode_button)
        root.addLayout(metadata_actions)
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.DoubleClicked | QTableWidget.EditKeyPressed)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        root.addWidget(self.table, 1)
        controls = QHBoxLayout()
        actions = [('إضافة عمود', self.add_column), ('حذف العمود', self.delete_column),
                   ('إعادة تسمية', self.rename_column), ('تحريك لأعلى', lambda: self.move_column(-1)),
                   ('تحريك لأسفل', lambda: self.move_column(1)), ('إخفاء العمود', self.toggle_column),
                   ('إظهار عمود', self.show_hidden_column)]
        for text, callback in actions:
            button = QPushButton(text)
            button.clicked.connect(callback)
            controls.addWidget(button)
        report_select = QPushButton('تبديل إدراج الصفوف المحددة في Word')
        report_select.clicked.connect(self.toggle_report_selection)
        controls.addWidget(report_select)
        root.addLayout(controls)
        self.report_selection_status = QLabel('الأهداف التي لم يخترها المستخدم لن تظهر في التقرير.')
        root.addWidget(self.report_selection_status)
        footer = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        footer.accepted.connect(self.accept)
        footer.rejected.connect(self.reject)
        root.addWidget(footer)
        self.refresh()

    def _visible(self):
        return [c for c in self.columns if c.get('visible', True)]

    def refresh(self):
        cols = self._visible()
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels([c.get('title', c['key']) for c in cols])
        self.table.setRowCount(len(self.regions))
        for row, target in enumerate(self.regions):
            for col, definition in enumerate(cols):
                value = value_for_column(target, definition)
                if value in ('', None) and definition.get('key') in self.metadata:
                    value = self.metadata[definition['key']]
                item = QTableWidgetItem(str(value))
                if definition.get('fixed') or definition['key'] == 'navigate':
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                self.table.setItem(row, col, item)

    def _selected_col(self):
        col = self.table.currentColumn()
        return col if 0 <= col < self.table.columnCount() else -1

    def add_column(self):
        self._commit_table_cells()
        title, ok = QInputDialog.getText(self, 'إضافة عمود', 'عنوان العمود الجديد:')
        if ok and title.strip():
            key = 'custom_' + uuid.uuid4().hex
            self.columns.append({'key': key, 'title': title.strip(), 'visible': True, 'fixed': False})
            for target in self.regions:
                target.setdefault('excel_values', {})[key] = ''
            self.refresh()

    def delete_column(self):
        self._commit_table_cells()
        col = self._selected_col()
        visible = self._visible()
        if col < 0:
            return
        definition = visible[col]
        if definition.get('fixed'):
            QMessageBox.information(self, 'محرر Excel', 'لا يمكن حذف العمود الثابت الذي يحمل المعرف أو الانتقال.')
            return
        if QMessageBox.question(self, 'حذف العمود', f"حذف العمود «{definition['title']}» من التقرير؟") != QMessageBox.Yes:
            return
        self.columns = [c for c in self.columns if c['key'] != definition['key']]
        self.refresh()

    def rename_column(self):
        self._commit_table_cells()
        col = self._selected_col()
        visible = self._visible()
        if col < 0:
            return
        current = visible[col]
        title, ok = QInputDialog.getText(self, 'إعادة تسمية العمود', 'العنوان:', text=current.get('title', ''))
        if ok and title.strip():
            current['title'] = title.strip()
            self.refresh()

    def move_column(self, delta):
        col = self._selected_col()
        visible = self._visible()
        target_col = col + delta
        if col < 0 or not 0 <= target_col < len(visible):
            return
        self._commit_table_cells()
        left, right = visible[col], visible[target_col]
        full_left = self.columns.index(left)
        full_right = self.columns.index(right)
        self.columns[full_left], self.columns[full_right] = self.columns[full_right], self.columns[full_left]
        self.refresh()
        self.table.setCurrentCell(0, target_col)

    def toggle_column(self):
        self._commit_table_cells()
        col = self._selected_col()
        visible = self._visible()
        if col < 0:
            return
        definition = visible[col]
        definition['visible'] = False
        if not any(c.get('visible', True) for c in self.columns):
            definition['visible'] = True
            QMessageBox.information(self, 'محرر Excel', 'يجب إبقاء عمود واحد ظاهرًا على الأقل.')
        self.refresh()

    def show_hidden_column(self):
        self._commit_table_cells()
        hidden = [c for c in self.columns if not c.get('visible', True)]
        if not hidden:
            QMessageBox.information(self, 'محرر Excel', 'لا توجد أعمدة مخفية.')
            return
        titles = [c.get('title', c['key']) for c in hidden]
        title, ok = QInputDialog.getItem(self, 'إظهار عمود', 'اختر عمودًا:', titles, 0, False)
        if ok:
            next(c for c in hidden if c.get('title', c['key']) == title)['visible'] = True
            self.refresh()

    def refresh_metadata_from_images(self):
        from image_location import extract_image_metadata
        extracted = []
        for path in self.image_paths[:2]:
            extracted.append(extract_image_metadata(path) if path else {})
        if not extracted:
            QMessageBox.information(self, 'استخراج البيانات', 'افتح الصور قبل وبعد أولاً.')
            return
        has_manual = any(self.metadata_sources.get(key) == 'user' and field.text().strip()
                         for key, field in self.meta_edits.items())
        overwrite = False
        if has_manual:
            overwrite = QMessageBox.question(
                self, 'تأكيد استبدال البيانات اليدوية',
                'توجد قيم أدخلها المستخدم. هل تسمح باستبدال هذه القيم بالبيانات المستخرجة؟',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes
        mapping = {}
        for index, role in enumerate(('before', 'after')):
            info = extracted[index] if index < len(extracted) else {}
            mapping[f'{role}_date'] = (info.get('capture_date', ''), info.get('source', ''))
            mapping[f'{role}_location'] = (info.get('location', ''), info.get('source', ''))
        usable = [info for info in extracted if info.get('longitude') is not None]
        if len(usable) == 2 and all(
                abs(usable[0][k] - usable[1][k]) < 1e-6 for k in ('longitude', 'latitude')):
            mapping['longitude'] = (usable[0]['longitude'], usable[0]['source'])
            mapping['latitude'] = (usable[0]['latitude'], usable[0]['source'])
            mapping['location'] = ('الموقع المشترك للصورتين: ' + usable[0]['location'], usable[0]['source'])
        elif len(usable) == 1 and sum(bool(info.get('path')) for info in extracted) == 1:
            mapping['longitude'] = (usable[0]['longitude'], usable[0]['source'])
            mapping['latitude'] = (usable[0]['latitude'], usable[0]['source'])
            mapping['location'] = (usable[0]['location'], usable[0]['source'])
        for key, (value, source) in mapping.items():
            if not value:
                continue
            field = self.meta_edits[key]
            if self.metadata_sources.get(key) == 'user' and field.text().strip() and not overwrite:
                continue
            field.setText(str(value))
            self.metadata_sources[key] = source
            field.setToolTip('المصدر: ' + source)
        self.image_sources = {'before': extracted[0] if extracted else {},
                              'after': extracted[1] if len(extracted) > 1 else {}}
        summaries = []
        for label, info in zip(('قبل', 'بعد'), extracted):
            if info.get('location'):
                summaries.append(f"{label}: {info.get('source')} / {info.get('location_kind')}")
            elif info:
                summaries.append(f'{label}: لا توجد إحداثيات موثقة')
        self.metadata_status.setText(' | '.join(summaries) or 'لم تُعثر على بيانات موقع أو تاريخ موثقة.')

    def lookup_place_names(self):
        coords = []
        for role, label in (('before', 'قبل'), ('after', 'بعد')):
            info = self.image_sources.get(role) or {}
            if info.get('longitude') is not None and info.get('latitude') is not None:
                coords.append((role, label, info['longitude'], info['latitude']))
        if not coords:
            QMessageBox.information(self, 'البحث عن الأماكن', 'لا توجد إحداثيات موثقة للبحث عنها.')
            return
        if QMessageBox.question(
                self, 'بحث اختياري عبر الإنترنت',
                'سيُرسل البرنامج إحداثيات الصور المحددة إلى خدمة OpenStreetMap Nominatim. '
                'الإنترنت اختياري وستُحفظ النتائج محلياً في ذاكرة مؤقتة. هل تريد المتابعة؟',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            from image_location import reverse_geocode
            from runtime_paths import get_storage_path
            cache_path = get_storage_path('root') / 'geocoding_cache.json'
            for role, label, lon, lat in coords:
                result = reverse_geocode(lon, lat, cache_path)
                value = result.get('display_name', '')
                if value:
                    field = self.meta_edits[f'{role}_location']
                    field.setText(value)
                    self.metadata_sources[f'{role}_location'] = result.get('source', 'OpenStreetMap Nominatim')
                    field.setToolTip('المصدر: ' + self.metadata_sources[f'{role}_location'])
            self.metadata_status.setText('تم البحث عن أسماء الأماكن؛ بقيت الإحداثيات الأصلية محفوظة.')
        except Exception as exc:
            QMessageBox.warning(self, 'تعذر البحث', f'لم يكتمل البحث عن اسم المكان. بقيت الإحداثيات دون تغيير.\n{exc}')

    def _commit_table_cells(self):
        cols = self._visible()
        for row, target in enumerate(self.regions):
            for col, definition in enumerate(cols):
                item = self.table.item(row, col)
                if item is not None:
                    set_column_value(target, definition, item.text())

    def toggle_report_selection(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        if not rows:
            QMessageBox.information(self, 'اختيار أهداف التقرير', 'حدد صفًا واحدًا أو أكثر أولاً.')
            return
        include = not all(self.regions[row].get('report_included', False) for row in rows)
        for row in rows:
            self.regions[row]['report_included'] = include
        state = 'أُدرجت' if include else 'استُبعدت'
        ids = '، '.join(str(self.regions[row].get('id', self.regions[row].get('target_uid', ''))) for row in rows)
        self.report_selection_status.setText(f'{state} {len(rows)} أهداف من اختيار المستخدم: {ids}')

    def accept(self):
        self._commit_table_cells()
        for key, field in self.meta_edits.items():
            value = field.text().strip()
            if value != self.metadata_initial.get(key, '') and value:
                self.metadata_sources[key] = 'user'
        self.metadata = {key: field.text().strip() for key, field in self.meta_edits.items()}
        self.metadata['_sources'] = self.metadata_sources
        self.metadata['_image_sources'] = self.image_sources
        super().accept()
