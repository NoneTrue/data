import sys, os, json, datetime, tempfile, shutil, glob, subprocess, gc, time
if sys.version_info[:3] != (3,11,9):
    raise RuntimeError('ChangeVision2026 requires Python 3.11.9 exactly.')

# Use a moderate Qt UI scale on Windows.
# This affects display scaling only; analysis and image coordinates are unchanged.
if sys.platform.startswith('win'):
    os.environ['QT_SCALE_FACTOR'] = '1.25'
    os.environ.setdefault('QT_FONT_DPI', '96')
from pathlib import Path
import runtime_paths
runtime_paths.configure_geospatial_environment()
from PySide6.QtCore import Qt, QRectF, Signal, QThread, QTimer
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QBrush, QAction, QColor, QFont, QPainterPath
import cv2
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QSlider, QSpinBox, QDoubleSpinBox, QGroupBox, QFormLayout, QTextEdit,
    QMessageBox, QStatusBar, QSplitter, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QComboBox, QTabWidget, QFrame, QInputDialog,
    QToolBar, QToolButton, QMenu, QProgressBar, QScrollArea, QGridLayout
)
import engine
from region_excel import RegionAutosave,RegionWorkbook,editable as excel_editable,identity as excel_identity
from report_manager import ReportManager, write_xlsx
import precise_registration
import gis_tools
import radiometric_inspection
from workflow_ui import WorkflowMixin
import preparation
from storage_manager import (
    StorageSettingsDialog, atomic_write, confirm_overwrite,
    get_storage_path, validate_storage_path,
)
from image_formats import ImageImportDialog, read_analysis_image
from target_management import (
    DEFAULT_COLUMNS, SpreadsheetEditorDialog, TargetManagerDialog,
    ensure_target_metadata, value_for_column, set_column_value,
)
from report_export import ReportBuilderDialog
from project_format import read_project, write_project

APP_TITLE = 'ChangeVision 2026 - Analysis Edition | نظام كشف التغيرات والمواءمة'

class ImageCanvas(QGraphicsView):
    mouse_moved = Signal(int, int)
    view_state_changed = Signal(object)
    image_clicked = Signal(float, float)

    def __init__(self, title='', accent='#2387ff'):
        super().__init__()
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.item = QGraphicsPixmapItem()
        self.scene_obj.addItem(self.item)

        # V42 HYBRID SHARP VIEW:
        # Smart Preview remains the lightweight base layer, while this second pixmap
        # shows ONLY the currently visible source crop at native resolution when zoomed in.
        self.detail_item = QGraphicsPixmapItem()
        self.detail_item.setZValue(50)
        self.detail_item.setVisible(False)
        self.scene_obj.addItem(self.detail_item)
        self.detail_provider = None
        self.detail_min_view_scale = 0.28   # screen pixels per original source pixel
        self.detail_max_side = 4096
        self._detail_timer = QTimer(self)
        self._detail_timer.setSingleShot(True)
        self._detail_timer.setInterval(90)
        self._detail_timer.timeout.connect(self._update_native_detail)

        self.array = None
        self.source_shape = None
        self.preview_scale = 1.0
        self.zoom_factor = 1.0
        self.fit_scale = 1.0
        self._sync_applying = False
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QBrush(QColor('#10151d')))
        self.setFrameStyle(QFrame.NoFrame)
        self.setStyleSheet(f'QGraphicsView{{border:2px solid {accent};background:#10151d;}}')
        self.horizontalScrollBar().valueChanged.connect(self._scroll_changed)
        self.verticalScrollBar().valueChanged.connect(self._scroll_changed)

    def set_detail_provider(self, provider):
        """Set callback provider(x0,y0,x1,y1)->RGB/gray native-resolution crop."""
        self.detail_provider = provider
        self._schedule_native_detail()

    def _hide_native_detail(self):
        self.detail_item.setVisible(False)
        self.detail_item.setPixmap(QPixmap())

    def _schedule_native_detail(self):
        if self.detail_provider is None or self.item.pixmap().isNull():
            self._hide_native_detail()
            return
        self._detail_timer.start()

    def _visible_source_rect(self):
        if self.item.pixmap().isNull():
            return None
        poly = self.mapToScene(self.viewport().rect())
        r = poly.boundingRect().intersected(self.sceneRect())
        if r.isEmpty():
            return None
        # Small margin avoids a visible seam while panning.
        margin = 12.0
        x0 = max(0, int(np.floor(r.left() - margin)))
        y0 = max(0, int(np.floor(r.top() - margin)))
        x1 = min(int(self.sceneRect().width()), int(np.ceil(r.right() + margin)) + 1)
        y1 = min(int(self.sceneRect().height()), int(np.ceil(r.bottom() + margin)) + 1)
        if x1 <= x0 or y1 <= y0:
            return None
        return x0, y0, x1, y1

    def _update_native_detail(self):
        """Overlay only the visible crop at 1:1 source resolution when zoomed enough."""
        try:
            if self.detail_provider is None or self.item.pixmap().isNull():
                self._hide_native_detail()
                return

            view_scale = abs(float(self.transform().m11()))
            if view_scale < float(self.detail_min_view_scale):
                self._hide_native_detail()
                return

            rect = self._visible_source_rect()
            if rect is None:
                self._hide_native_detail()
                return
            x0, y0, x1, y1 = rect
            cw, ch = x1 - x0, y1 - y0
            if cw > self.detail_max_side or ch > self.detail_max_side:
                self._hide_native_detail()
                return

            crop = self.detail_provider(x0, y0, x1, y1)
            if crop is None:
                self._hide_native_detail()
                return
            a = np.ascontiguousarray(crop)
            if a.ndim == 2:
                q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_Grayscale8).copy()
            else:
                if a.shape[2] > 3:
                    a = np.ascontiguousarray(a[:, :, :3])
                q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGB888).copy()

            self.detail_item.setPixmap(QPixmap.fromImage(q))
            self.detail_item.setScale(1.0)
            self.detail_item.setPos(float(x0), float(y0))
            self.detail_item.setVisible(True)
        except Exception:
            self._hide_native_detail()

    def set_array(self, arr):
        # V31 SMART PREVIEW: the canvas never creates a full-resolution QImage/QPixmap
        # for huge rasters.  The original numpy array passed by the analysis engine is
        # NOT modified and remains full resolution for alignment/change analysis.
        # The graphics item is scaled back to the original pixel coordinate space so
        # mouse/navigation coordinates continue to refer to full-resolution pixels.
        if arr is None:
            self.array = None
            self.source_shape = None
            self.preview_scale = 1.0
            self.item.setPixmap(QPixmap())
            self.item.setScale(1.0)
            self._hide_native_detail()
            return

        src = np.asarray(arr)
        h, w = src.shape[:2]
        self.source_shape = (int(h), int(w))
        max_side = 4096
        scale = min(1.0, float(max_side) / float(max(h, w)))
        if scale < 0.999:
            pw = max(1, int(round(w * scale)))
            ph = max(1, int(round(h * scale)))
            a = cv2.resize(src, (pw, ph), interpolation=cv2.INTER_AREA)
        else:
            a = src
        self.array = np.ascontiguousarray(a)
        self.preview_scale = float(scale)
        a = self.array
        if a.ndim == 2:
            q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_Grayscale8).copy()
        else:
            q = QImage(a.data, a.shape[1], a.shape[0], a.strides[0], QImage.Format_RGB888).copy()
        self.item.setPixmap(QPixmap.fromImage(q))
        # Keep scene coordinates equal to original full-resolution pixel coordinates.
        item_scale = (1.0 / scale) if scale > 0 else 1.0
        self.item.setScale(item_scale)
        self.scene_obj.setSceneRect(QRectF(0.0, 0.0, float(w), float(h)))
        self._hide_native_detail()
        self.fit_image(emit=False)

    def _view_state(self):
        if self.item.pixmap().isNull():
            return None
        r = self.sceneRect()
        if r.width() <= 0 or r.height() <= 0:
            return None
        c = self.mapToScene(self.viewport().rect().center())
        nx = (c.x() - r.left()) / r.width()
        ny = (c.y() - r.top()) / r.height()
        current_scale = abs(float(self.transform().m11()))
        base = max(abs(float(self.fit_scale)), 1e-12)
        return {
            'cx': float(c.x()), 'cy': float(c.y()), 'scene_scale': current_scale,
            'nx': float(max(0.0, min(1.0, nx))),
            'ny': float(max(0.0, min(1.0, ny))),
            'zoom_ratio': float(max(0.01, min(500.0, current_scale / base))),
        }

    def _emit_view_state(self):
        if self._sync_applying:
            return
        st = self._view_state()
        if st is not None:
            self.view_state_changed.emit(st)

    def _scroll_changed(self, _value):
        self._emit_view_state()
        self._schedule_native_detail()

    def apply_view_state(self, state):
        if not state or self.item.pixmap().isNull():
            return
        self._sync_applying = True
        try:
            r = self.sceneRect()
            if 'scene_scale' in state:
                target_scale = max(1e-12, float(state['scene_scale']))
                cur = max(1e-12, abs(float(self.transform().m11())))
                self.scale(target_scale / cur, target_scale / cur)
                self.centerOn(float(state['cx']), float(state['cy']))
                self.zoom_factor = target_scale / max(self.fit_scale, 1e-12)
                self._schedule_native_detail()
                return
            nx = float(state.get('nx', 0.5)); ny = float(state.get('ny', 0.5))
            zr = max(0.01, min(500.0, float(state.get('zoom_ratio', 1.0))))
            target_scale = max(1e-12, abs(float(self.fit_scale)) * zr)
            cur = max(1e-12, abs(float(self.transform().m11())))
            self.scale(target_scale / cur, target_scale / cur)
            self.zoom_factor = zr
            self.centerOn(r.left() + nx * r.width(), r.top() + ny * r.height())
        finally:
            self._sync_applying = False
        self._schedule_native_detail()

    def fit_image(self, emit=True):
        if not self.item.pixmap().isNull():
            self._sync_applying = True
            try:
                self.fitInView(self.item, Qt.KeepAspectRatio)
                self.fit_scale = max(abs(float(self.transform().m11())), 1e-12)
                self.zoom_factor = 1.0
            finally:
                self._sync_applying = False
            if emit:
                self._emit_view_state()
            self._schedule_native_detail()

    def actual_size(self, emit=True):
        if self.item.pixmap().isNull():
            return
        self._sync_applying = True
        try:
            self.resetTransform()
            self.zoom_factor = 1.0 / max(self.fit_scale, 1e-12)
        finally:
            self._sync_applying = False
        if emit:
            self._emit_view_state()
        self._schedule_native_detail()

    def zoom_by(self, factor, emit=True):
        if self.item.pixmap().isNull():
            return
        self._sync_applying = True
        try:
            self.scale(float(factor), float(factor))
            self.zoom_factor = abs(float(self.transform().m11())) / max(self.fit_scale, 1e-12)
        finally:
            self._sync_applying = False
        if emit:
            self._emit_view_state()
        self._schedule_native_detail()

    def wheelEvent(self, event):
        if self.item.pixmap().isNull():
            return
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        # AnchorUnderMouse keeps the inspected feature under the cursor; the resulting
        # normalized center/zoom are then mirrored to BEFORE and AFTER by MainWindow.
        cursor = event.position().toPoint()
        before = self.mapToScene(cursor)
        self.zoom_by(factor, emit=False)
        after = self.mapToScene(cursor)
        center = self.mapToScene(self.viewport().rect().center())
        self._sync_applying = True
        try: self.centerOn(center + before - after)
        finally: self._sync_applying = False
        self._emit_view_state()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.array is not None:
            pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            p = self.mapToScene(pos)
            x, y = int(p.x()), int(p.y())
            sh = self.source_shape or self.array.shape[:2]
            if 0 <= x < int(sh[1]) and 0 <= y < int(sh[0]):
                self.mouse_moved.emit(x, y)
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        self._click_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and hasattr(self, '_click_start') and (event.position().toPoint()-self._click_start).manhattanLength() < 4:
            point = self.mapToScene(event.position().toPoint())
            self.image_clicked.emit(point.x(), point.y())
        self._emit_view_state()
        self._schedule_native_detail()

class HistogramWidget(QWidget):
    def __init__(self):
        super().__init__(); self.values = None; self.setMinimumHeight(150)
    def set_histogram(self, values): self.values = values; self.update()
    def paintEvent(self, event):
        p = QPainter(self); p.fillRect(self.rect(), QColor('#111720'))
        p.setPen(QPen(QColor('#596677'))); p.drawRect(self.rect().adjusted(0,0,-1,-1))
        if self.values is None or len(self.values)==0: return
        vals = np.asarray(self.values, dtype=float); mx = max(float(vals.max()), 1.0)
        w, h = self.width()-20, self.height()-28; bw = w / len(vals)
        for i, v in enumerate(vals):
            bh = (v/mx)*h; x=10+i*bw; y=10+h-bh
            c = QColor.fromHsv(int(220 - 210*(i/max(1,len(vals)-1))), 220, 245)
            p.fillRect(int(x), int(y), max(1,int(bw-1)), int(bh), c)
        p.setPen(QColor('#aab6c5')); p.drawText(10,self.height()-6,'0'); p.drawText(self.width()-35,self.height()-6,'255')

class MetricCard(QFrame):
    def __init__(self, title, value='—', accent='#24d26a'):
        super().__init__(); self.setObjectName('metricCard')
        lay=QHBoxLayout(self); lay.setContentsMargins(10,7,10,7)
        self.t=QLabel(title); self.v=QLabel(value); self.v.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
        self.v.setStyleSheet(f'font-size:18px;font-weight:700;color:{accent};')
        lay.addWidget(self.t,1); lay.addWidget(self.v)
    def set_value(self, text): self.v.setText(str(text))


class CooperativeWorker(QThread):
    def checkpoint(self):
        preparation.check_cancel(self.isInterruptionRequested)

    def checked_emit(self, signal, *args):
        self.checkpoint(); signal.emit(*args)


class RadiometricInspectWorker(CooperativeWorker):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, reference, moving, alignment=None, parent=None):
        super().__init__(parent)
        self.reference = reference
        self.moving = moving
        self.alignment = alignment

    def run(self):
        try:
            self.checkpoint()
            result = radiometric_inspection.inspect_radiometry(
                self.reference, self.moving, alignment=self.alignment)
            self.checked_emit(self.completed, result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.alignment = None


class AlignWorker(CooperativeWorker):
    progress = Signal(int, str)
    completed = Signal(object, object, object, object)
    failed = Signal(str)

    def __init__(self, reference, moving, preferred_ratio, parent=None):
        super().__init__(parent)
        self.reference = reference
        self.moving = moving
        self.preferred_ratio = preferred_ratio

    def run(self):
        try:
            self.checkpoint()
            aligned, H, report, valid_mask = engine.align_advanced(
                self.reference, self.moving, self.preferred_ratio,
                progress=lambda v, t: self.checked_emit(self.progress, v, t)
            )
            self.reference = None
            self.moving = None
            self.checked_emit(self.completed, aligned, H, report, valid_mask)
        except Exception as exc:
            self.failed.emit(str(exc))

class CompareWorker(CooperativeWorker):
    progress = Signal(int, str)
    completed = Signal(object, object)
    failed = Signal(str)

    def __init__(self, reference, moving, method, valid_mask, edge_tolerance=3, parent=None):
        super().__init__(parent)
        self.reference = reference
        self.moving = moving
        self.method = method
        self.valid_mask = valid_mask
        self.edge_tolerance = edge_tolerance

    def run(self):
        try:
            self.checkpoint()
            self.checked_emit(self.progress, 10, 'بدء المقارنة...')
            diff, meta = engine.compare(
                self.reference, self.moving, self.method,
                valid_mask=self.valid_mask, edge_tolerance=self.edge_tolerance
            )
            self.checked_emit(self.progress, 95, 'تجهيز نتيجة المقارنة...')
            self.reference = None
            self.moving = None
            self.valid_mask = None
            self.checked_emit(self.completed, diff, meta)
        except Exception as exc:
            self.failed.emit(str(exc))


class ChangeWorker(CooperativeWorker):
    progress = Signal(int, str)
    completed = Signal(object, object, object)
    failed = Signal(str)

    def __init__(self, diff, threshold, min_area, kernel, valid_mask, image_data,
                 method='ROBUST FINAL', edge_tolerance=3, reference=None, moving=None,
                 accept_confidence=60, review_confidence=45, merge_distance=12, parent=None):
        super().__init__(parent)
        self.diff = diff
        self.threshold = threshold
        self.min_area = min_area
        self.kernel = kernel
        self.valid_mask = valid_mask
        self.image_data = image_data
        self.method = method
        self.edge_tolerance = edge_tolerance
        self.reference = reference
        self.moving = moving
        self.accept_confidence = accept_confidence
        self.review_confidence = review_confidence
        self.merge_distance = merge_distance

    def run(self):
        try:
            self.checkpoint()
            method = str(self.method).upper()
            self.checked_emit(self.progress, 10, 'تشغيل ROBUST V6 الأساسي...')
            if method in ('ROBUST FINAL','FINAL POSTFILTER','ROBUST V6'):
                base_mask = engine.change_mask_v6(
                    self.diff, self.threshold, self.min_area, self.kernel,
                    valid_mask=self.valid_mask, edge_tolerance=self.edge_tolerance
                )
            elif method == 'ROBUST V5':
                base_mask = engine.change_mask_v5(
                    self.diff, self.threshold, self.min_area, self.kernel,
                    valid_mask=self.valid_mask, edge_tolerance=self.edge_tolerance
                )
            elif method == 'ROBUST V4':
                base_mask = engine.change_mask_v4(
                    self.diff, self.threshold, self.min_area, self.kernel,
                    valid_mask=self.valid_mask, edge_tolerance=self.edge_tolerance
                )
            elif method == 'ROBUST V3':
                base_mask = engine.change_mask_v3(
                    self.diff, self.threshold, self.min_area, self.kernel,
                    valid_mask=self.valid_mask, edge_tolerance=self.edge_tolerance
                )
            else:
                base_mask = engine.change_mask(
                    self.diff, self.threshold, self.min_area, self.kernel,
                    valid_mask=self.valid_mask
                )

            if method in ('ROBUST FINAL','FINAL POSTFILTER'):
                if self.reference is None or self.moving is None:
                    raise RuntimeError('Post-Filter يحتاج الصورتين بعد المواءمة.')
                self.checked_emit(self.progress, 48, 'تحليل مرشحات V6 وحساب الثقة...')
                mask, regions, summary = engine.postfilter_v6_candidates(
                    base_mask, self.diff, self.reference, self.moving,
                    image_data=self.image_data, valid_mask=self.valid_mask,
                    edge_tolerance=self.edge_tolerance,
                    accept_confidence=self.accept_confidence,
                    review_confidence=self.review_confidence
                )
                # base_mask is no longer needed after the V6 Post-Filter.
                # Releasing it here saves one full-resolution mask in RAM.
                del base_mask
                self.checked_emit(self.progress, 85, 'حساب الإحصائيات النهائية...')
                st = engine.stats(mask, self.image_data, self.diff, valid_mask=self.valid_mask)
                self.checked_emit(self.progress, 90, 'تجميع أجزاء التغيير وعدّ المجموعات...')
                # V32 RAM SAFE: use the accepted mask itself; do not create a second
                # full-resolution candidate_mask copy. The algorithm and pixels are unchanged.
                candidate_mask = mask
                del mask
                candidate_stats = engine.stats(candidate_mask, self.image_data, self.diff, valid_mask=self.valid_mask)
                regions, change_objects, object_summary = engine.group_change_regions(
                    candidate_mask, regions, reference=self.reference, moving=self.moving,
                    image_data=self.image_data, valid_mask=self.valid_mask,
                    merge_distance=self.merge_distance
                )
                # Final reported mask contains only bidirectionally verified real changes.
                candidate_shape = candidate_mask.shape
                del candidate_mask
                mask = engine.verified_mask_from_objects(candidate_shape, change_objects)
                st = engine.stats(mask, self.image_data, self.diff, valid_mask=self.valid_mask)
                st['candidate_changed_percent'] = candidate_stats.get('changed_percent',0.0)
                st['candidate_changed_pixels'] = candidate_stats.get('changed_pixels',0)
                st.update(summary)
                st.update(object_summary)
                st['change_objects'] = change_objects
            else:
                mask = base_mask
                self.checked_emit(self.progress, 62, 'حساب مناطق التغيير...')
                regions = engine.region_table(mask, self.diff, self.image_data)
                self.checked_emit(self.progress, 85, 'حساب الإحصائيات...')
                st = engine.stats(mask, self.image_data, self.diff, valid_mask=self.valid_mask)
            if method == 'STRUCTURAL':
                for region in regions:
                    region.update(change_type='تغير بنيوي/موضعي', review_status='يحتاج مراجعة',classification='مراجعة / غير مؤكد')
                st.update(structural_count=len(regions), verification_review_count=len(regions),
                          comparison_mode='STRUCTURAL', interpretation='مرشحات بنيوية تحتاج مراجعة؛ لا تمثل ظهورًا أو اختفاءً مؤكدًا')
            # Drop all full-resolution input references owned by the worker before
            # returning the result to the GUI. MainWindow keeps the authoritative inputs.
            self.diff = None
            self.valid_mask = None
            self.image_data = None
            self.reference = None
            self.moving = None
            self.checked_emit(self.completed, mask, regions, st)
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(WorkflowMixin, QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE); self.resize(1780, 1020)
        runtime_paths.apply_application_icon(QApplication.instance(),self)
        self.runtime_dependencies=runtime_paths.dependency_status()
        self.a=self.b=None; self.aligned=None; self.H=None; self.valid_mask=None; self.analysis_valid_mask=None; self.diff=None; self.mask=None
        self.compare_meta={}; self.regions=[]; self.change_objects=[]; self.current_project=None
        self.report_columns=[dict(column) for column in DEFAULT_COLUMNS]
        self.report_metadata={}
        self.region_autosave=RegionAutosave(self)
        self.region_autosave.status.connect(lambda text:self.statusBar().showMessage(text))
        self.region_autosave.saved.connect(self._excel_saved)
        self.region_autosave.failed.connect(self._excel_failed)
        self._close_after_excel=False
        self._region_table_loading=False
        self._pending_project_analysis=None
        self.arcgis_aligned_path = None
        self.arcgis_diff_path = None
        self.arcgis_mask_path = None
        self.arcgis_vector_path = None
        self._gis_overlap_reference_path = None
        self._gis_overlap_moving_path = None
        documents_dir = runtime_paths.get_user_data_path()
        validate_storage_path(documents_dir, create=True)
        project_dir = str(documents_dir)
        self._cleanup_stale_temp_files()

        # Analysis-only edition: one report stream and no ArcGIS/QGIS Python dependency.
        self._report_storage_path=get_storage_path('reports')
        self._excel_storage_path=get_storage_path('excel')
        self.reporter = ReportManager(project_dir, APP_TITLE, report_group='Analysis_Results',
                                      report_root=self._report_storage_path,
                                      excel_root=self._excel_storage_path)
        self.arcgis_reporter = None

        self._align_worker = None
        self._compare_worker = None
        self._change_worker = None
        self._radiometric_inspect_worker = None
        self._radiometric_inspect_context = None
        self.last_alignment_report = {}
        self.last_arcgis_alignment_report = {}
        self.last_change_stats = {}
        self.last_arcgis_change_stats = {}
        self.arcgis_regions = []
        self._arcgis_change_active = False
        self._init_workflow()
        self._build_ui(); self._apply_style(); self._update_device()
        self._refresh_readiness()
        self.log('النظام جاهز. افتح الصورتين للبدء. التقارير النصية وExcel تحفظ تلقائيًا بعد كل عملية.')
        self.log('V42: العرض الهجين مفعل — Preview سريع عند العرض العام + Full Resolution للجزء المرئي عند التكبير.')
        self.log('مجلد جلسة التحليل: ' + str(self.reporter.session_dir))
        self.refresh_reports_panel()

    def _build_ui(self):
        self._build_toolbar()
        root=QWidget(); self.setCentralWidget(root); outer=QVBoxLayout(root); outer.setContentsMargins(5,5,5,5); outer.setSpacing(5)
        outer.addWidget(self._workflow_bar())
        main_split=QSplitter(Qt.Horizontal); outer.addWidget(main_split,1)
        main_split.addWidget(self._build_left_panel())
        main_split.addWidget(self._build_center_panel())
        main_split.addWidget(self._build_right_panel())
        main_split.setSizes([280,1180,340]); main_split.setChildrenCollapsible(False)
        self.setStatusBar(QStatusBar())
        self.status_coord=QLabel('X: —   Y: —'); self.status_img=QLabel('الصورة: —'); self.status_crs=QLabel('CRS: —')
        self.device_lbl=QLabel('CPU'); self.mem_progress=QProgressBar(); self.mem_progress.setRange(0,100); self.mem_progress.setValue(20); self.mem_progress.setMaximumWidth(130)
        self.task_progress=QProgressBar(); self.task_progress.setRange(0,100); self.task_progress.setValue(0); self.task_progress.setMaximumWidth(240); self.task_progress.setVisible(False)
        for w in (self.status_crs,self.status_img,self.status_coord,self.device_lbl,self.mem_progress,self.task_progress): self.statusBar().addPermanentWidget(w)

    def _build_toolbar(self):
        tb=QToolBar('الرئيسي'); tb.setMovable(False); tb.setIconSize(tb.iconSize()); self.addToolBar(tb)
        def add(text, fn, tip=''):
            a=QAction(text,self); a.triggered.connect(fn); a.setToolTip(tip or text); tb.addAction(a); return a
        add('📂 فتح صورة 1', self.open_a); add('📂 فتح صورة 2', self.open_b); add('💾 حفظ مشروع', self.save_project); add('📁 فتح مشروع', self.open_project)
        self.undo_action=add('رجوع خطوة',self.workflow_undo)
        self.stop_action=add('إيقاف العملية',self.workflow_stop)
        primary_toolbar=tb
        advanced=QToolButton();advanced.setText('أدوات متقدمة');advanced.setPopupMode(QToolButton.InstantPopup)
        menu=QMenu(advanced);advanced.setMenu(menu);tb.addWidget(advanced)
        tb=menu
        tb.addSeparator(); add('▣ مواءمة/مطابقة', self.align); add('↻ إعادة ضبط', self.reset_analysis); add('🆕 جلسة جديدة', self.new_full_session)
        tb.addSeparator(); self.compare_action=add('≋ مقارنة', self.run_compare); self.detect_action=add('⚡ كشف التغيير', self.detect_changes)
        tb.addSeparator(); add('▥ Side by Side', lambda:self.set_view_mode('side')); add('◐ Overlay', lambda:self.set_view_mode('overlay')); add('◫ Difference', lambda:self.set_view_mode('difference')); add('🔥 Change Map', lambda:self.set_view_mode('change'))
        self.annotate_action=QAction('🏷 الأرقام والأنواع',self); self.annotate_action.setCheckable(True); self.annotate_action.setChecked(True); self.annotate_action.setToolTip('إظهار رقم ونوع وثقة كل مجموعة تغيير على Change Map'); self.annotate_action.toggled.connect(self.refresh_result_view); tb.addAction(self.annotate_action)
        self.sync_action=QAction('🔗 مزامنة النوافذ',self); self.sync_action.setCheckable(True); self.sync_action.setChecked(True); self.sync_action.setToolTip('تشغيل/إيقاف المزامنة. CHANGE MAP هو الإطار المرجعي للتحريك والتكبير والتصغير.'); self.sync_action.toggled.connect(self._toggle_view_sync); tb.addAction(self.sync_action)
        self.sync_action.setText('Sync View');self.sync_action.setToolTip('Zoom وPan من أي شاشة حسب الموقع الجغرافي الصحيح')
        primary_toolbar.addAction(self.sync_action)
        self.side_by_side_export_action=QAction('🖼 حفظ قبل/بعد',self); self.side_by_side_export_action.setToolTip('حفظ ما يظهر حاليًا في نافذتي BEFORE وAFTER جنبًا إلى جنب كصورة مستقلة PNG/JPG/TIFF'); self.side_by_side_export_action.triggered.connect(self.save_before_after_side_by_side); tb.addAction(self.side_by_side_export_action)
        tb.addSeparator(); add('📊 فتح التقارير', self.open_reports_folder, 'فتح مجلد تقارير الجلسة الحالية'); add('📝 حفظ تقرير الآن', self.save_report_now, 'حفظ TXT وExcel فورًا'); add('📄 إعداد تقرير Word/PDF', self.open_report_builder, 'معاينة الأقسام وترتيبها وتصدير التقرير')
        tb.addSeparator(); add('🧰 GIS أدوات', self._show_gis_tools_tab); add('⬆ تصدير النتائج', self.export_all)
        tb.addSeparator(); add('⚙ إدارة التخزين ومسارات الحفظ', self.manage_storage)
        add('🖼 استيراد وفحص صور متعددة', self.import_image_batch)

    def import_image_batch(self):
        dialog=ImageImportDialog(self)
        if dialog.exec()==QDialog.Accepted and dialog.selected_path:
            try:
                if dialog.selected_role=='before':self._load_a(dialog.selected_path)
                else:self._load_b(dialog.selected_path)
            except Exception as exc:
                self.err(exc)

    def manage_storage(self):
        dialog=StorageSettingsDialog(self)
        if dialog.exec()!=QDialog.Accepted:
            return
        selected=get_storage_path('reports')
        selected_excel=get_storage_path('excel')
        if selected!=self._report_storage_path or selected_excel!=getattr(self,'_excel_storage_path',get_storage_path('excel')):
            try:
                validate_storage_path(selected,create=True)
                reporter=ReportManager(str(runtime_paths.get_user_data_path()),APP_TITLE,
                                       report_group='Analysis_Results',report_root=selected,
                                       excel_root=selected_excel)
                self.reporter=reporter
                self._report_storage_path=selected
                self._excel_storage_path=selected_excel
            except Exception as exc:
                QMessageBox.warning(self,'تعذر تفعيل مسار التقارير',
                                    f'تعذر الكتابة إلى المسار المحدد. تحقق من القرص أو الصلاحيات ثم أعد المحاولة.\n{exc}')

    def _build_left_panel(self):
        panel=QWidget(); lay=QVBoxLayout(panel); lay.setContentsMargins(2,2,2,2)
        layers=QGroupBox('الطبقات'); ll=QVBoxLayout(layers)
        self.layer_a=QCheckBox('🟩 الصورة قبل (Image 1)'); self.layer_b=QCheckBox('🟦 الصورة بعد (Image 2)'); self.layer_change=QCheckBox('🟥 خريطة التغيير'); self.layer_poly=QCheckBox('🟨 مناطق التغيير')
        for c in (self.layer_a,self.layer_b,self.layer_change,self.layer_poly): c.setChecked(True); ll.addWidget(c)
        ll.addWidget(QLabel('شفافية الـ Overlay'))
        self.opacity=QSlider(Qt.Horizontal); self.opacity.setRange(0,100); self.opacity.setValue(50); self.opacity.valueChanged.connect(self.refresh_result_view); ll.addWidget(self.opacity)
        lay.addWidget(layers)
        legend=QGroupBox('مفتاح التحقق الثنائي')
        lg=QVBoxLayout(legend)
        for txt,col in [('🟩 موجود في الصورتين – لا تغير حقيقي','#38c65a'),('🟥 اختفاء حقيقي – موجود قبل فقط','#ff4b4b'),('🟧 ظهور حقيقي – موجود بعد فقط','#ff9f1a'),('🟦 تغير بنيوي/موضعي','#2a9df4'),('🟪 ظل/حافة تابع لجسم مطابق – مستبعد','#b66cff'),('⬜ مراجعة / غير مؤكد','#aab6c5')]:
            lab=QLabel(txt); lab.setStyleSheet(f'color:{col};font-weight:700;padding:2px;'); lg.addWidget(lab)
        lay.addWidget(legend)
        tools=QGroupBox('أدوات العرض'); gl=QGridLayout(tools)
        buttons=[('🔍 تكبير',lambda:self.zoom_all(1.25)),('🔎 تصغير',lambda:self.zoom_all(0.8)),('⛶ ملاءمة',self.fit_all),('100%',self.actual_all),('✋ تحريك',lambda:None),('🎯 مركز',self.fit_all)]
        for i,(t,f) in enumerate(buttons): b=QPushButton(t); b.clicked.connect(f); gl.addWidget(b,i//2,i%2)
        lay.addWidget(tools)
        disp=QGroupBox('إعدادات العرض'); fl=QFormLayout(disp)
        self.mode_combo=QComboBox(); self.mode_combo.addItems(['Side by Side','Overlay','Difference','Change Map']); self.mode_combo.currentTextChanged.connect(self._mode_combo_changed)
        self.mode_combo.setCurrentText('Change Map')
        fl.addRow('نمط العرض',self.mode_combo)
        self.palette_combo=QComboBox(); self.palette_combo.addItems(['Inferno','Gray']); self.palette_combo.currentTextChanged.connect(self.refresh_result_view); fl.addRow('لوحة الفرق',self.palette_combo)
        self.boundary_combo=QComboBox(); self.boundary_combo.addItems(['مضلعات','مربعات','بدون حدود']); self.boundary_combo.setCurrentText('مضلعات'); self.boundary_combo.currentTextChanged.connect(self._boundary_mode_changed); fl.addRow('حدود التغييرات',self.boundary_combo)
        self.sync_badge=QLabel('🔗 المزامنة: تشغيل | المرجع: Change Map'); self.sync_badge.setStyleSheet('color:#6ee7b7;font-weight:700;padding:5px;'); self.sync_badge.setWordWrap(True); fl.addRow('التحكم الموحد',self.sync_badge)
        self.sync_badge.setText('🔗 Sync View: تشغيل | التحكم من أي شاشة')
        lay.addWidget(disp)
        info=QGroupBox('بيانات الصورة'); il=QFormLayout(info)
        self.meta_size=QLabel('—'); self.meta_crs=QLabel('—'); self.meta_px=QLabel('—')
        il.addRow('الحجم',self.meta_size); il.addRow('CRS',self.meta_crs); il.addRow('Pixel Size',self.meta_px); lay.addWidget(info)
        panel.setMinimumWidth(245); lay.addStretch(1); return panel

    def _build_center_panel(self):
        panel=QWidget(); lay=QVBoxLayout(panel); lay.setContentsMargins(2,2,2,2)
        top=QSplitter(Qt.Horizontal)
        self.viewer_splitter=top
        top.setChildrenCollapsible(False)
        self.before_box,self.va=self._viewer_box('BEFORE – صورة قبل','#16c968')
        self.after_box,self.vb=self._viewer_box('AFTER – صورة بعد','#1688ff')
        self.change_box,self.vr=self._viewer_box('CHANGE MAP – خريطة التغيير','#ff3a3a')

        # V42: sharp native-resolution visible crops, without a second full-size pixmap.
        self.va.set_detail_provider(lambda x0,y0,x1,y1: self._native_detail_crop('before',x0,y0,x1,y1))
        self.vb.set_detail_provider(lambda x0,y0,x1,y1: self._native_detail_crop('after',x0,y0,x1,y1))
        self.vr.set_detail_provider(lambda x0,y0,x1,y1: self._native_detail_crop('change',x0,y0,x1,y1))
        top.addWidget(self.before_box);top.addWidget(self.after_box);top.addWidget(self.change_box);top.setSizes([1,1,1]); lay.addWidget(top,4)
        self.va.mouse_moved.connect(self._coords); self.vb.mouse_moved.connect(self._coords); self.vr.mouse_moved.connect(self._coords)
        # CHANGE MAP is the master viewport while synchronization is enabled.
        for view in (self.va,self.vb,self.vr):
            view.view_state_changed.connect(lambda state, source=view:self._workflow_sync_views(source,state))
        self.vr.image_clicked.connect(self.workflow_select_at)
        bottom=QSplitter(Qt.Horizontal)
        bottom.addWidget(self._build_regions()); bottom.addWidget(self._build_analysis_tabs()); bottom.addWidget(self._build_log())
        bottom.setSizes([420,520,420]); lay.addWidget(bottom,2); return panel

    def _viewer_box(self,title,accent):
        box=QGroupBox(title); l=QVBoxLayout(box); v=ImageCanvas(accent=accent); l.addWidget(v,1)
        controls=QHBoxLayout()
        for name,fn in [('100%',v.actual_size),('Center',v.fit_image)]:
            button=QPushButton(name);button.clicked.connect(lambda _,action=fn:action());controls.addWidget(button)
        box.full_view_button=QPushButton('Full View');box.full_view_button.clicked.connect(lambda:self.workflow_full_view(box));controls.addWidget(box.full_view_button)
        l.addLayout(controls)
        foot=QLabel('X: —   Y: —     Pixel: —'); foot.setObjectName('viewerFooter'); l.addWidget(foot)
        foot.setWordWrap(True)
        v.footer=foot; return box,v

    def _build_regions(self):
        box=QGroupBox('نتائج التغيير'); l=QVBoxLayout(box)
        self.results_source_label=QLabel('النتائج الحالية: التحليل')
        self.results_source_label.setAlignment(Qt.AlignCenter)
        self.results_source_label.setMinimumHeight(30)
        self.results_source_label.setStyleSheet(
            'background:#162333;border:1px solid #35506c;border-radius:5px;'
            'padding:5px 10px;color:#8bd5ff;font-weight:700;font-size:13px;'
        )
        l.addWidget(self.results_source_label)

        tabs=QTabWidget()
        # Keep the navigation control as the first, always-visible column.  The rest of
        # the application stays RTL, but this data table is explicitly LTR so Qt does not
        # move the last logical column (navigation) to the hidden far side on Windows.
        self.table=QTableWidget(0,11); self.table.setHorizontalHeaderLabels(['انتقال','ID','المجموعة','المساحة(px)','المساحة','Longitude','Latitude','متوسط الشدة','أعلى شدة','الثقة %','التصنيف'])
        self.table.setLayoutDirection(Qt.LeftToRight)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self._target_selection_order=[]
        self.table.selectionModel().selectionChanged.connect(self._track_target_selection_order)
        self.table.setEditTriggers(QTableWidget.DoubleClicked|QTableWidget.EditKeyPressed)
        self.table.itemChanged.connect(self._region_cell_committed)
        # V21: use mouse press as the single navigation trigger. The actual viewer move is
        # deferred to the next Qt event-loop turn so the table's own selection handling
        # cannot overwrite or interrupt the Change Map transform.
        self.table.cellPressed.connect(self._focus_change_row)
        self.table.setFocusPolicy(Qt.StrongFocus)
        region_page=QWidget();region_layout=QVBoxLayout(region_page);region_layout.addWidget(self.table)
        review_row=QHBoxLayout()
        self.review_result_button=QPushButton('مراجعة النتيجة المحددة')
        self.review_result_button.clicked.connect(self.workflow_review_result);review_row.addWidget(self.review_result_button)
        self.delete_result_button=QPushButton('استبعاد النتيجة المحددة')
        self.delete_result_button.clicked.connect(self.workflow_delete_result);review_row.addWidget(self.delete_result_button)
        self.manage_targets_button=QPushButton('إدارة الأهداف والتقرير')
        self.manage_targets_button.clicked.connect(self.manage_targets);review_row.addWidget(self.manage_targets_button)
        self.edit_excel_button=QPushButton('محرر Excel')
        self.edit_excel_button.clicked.connect(self.edit_region_spreadsheet);review_row.addWidget(self.edit_excel_button)
        region_layout.addLayout(review_row)
        report_row=QHBoxLayout()
        self.add_report_image_button=QPushButton('إضافة المحدد للتقرير')
        self.add_report_image_button.setToolTip('اختر عدة صفوف ثم أضف قصاصات BEFORE أو AFTER أو CHANGE MAP إلى تقرير Word')
        self.add_report_image_button.clicked.connect(self.add_selected_targets_to_report)
        report_row.addWidget(self.add_report_image_button)
        self.clear_target_selection_button=QPushButton('إلغاء تحديد الصفوف')
        self.clear_target_selection_button.clicked.connect(self.table.clearSelection)
        report_row.addWidget(self.clear_target_selection_button)
        self.exclude_report_targets_button=QPushButton('استبعاد المحدد من التقرير')
        self.exclude_report_targets_button.clicked.connect(self.exclude_selected_targets_from_report)
        report_row.addWidget(self.exclude_report_targets_button)
        region_layout.addLayout(report_row);tabs.addTab(region_page,'المناطق')
        self.object_table=QTableWidget(0,13)
        self.object_table.setHorizontalHeaderLabels(['المجموعة','النتيجة النهائية','الثقة %','وجود BEFORE %','وجود AFTER %','تشابه الوجود %','مطابقة الجسم %','تداخل الحواف %','احتمال ظل %','المساحة(px)','الأجزاء','المركز X,Y','ملاحظة التحقق'])
        self.object_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.object_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.object_table.cellPressed.connect(lambda row,col:self.workflow_focus_region(self.change_objects[row],row) if row<len(self.change_objects) else None)
        tabs.addTab(self.object_table,'ما الذي اختلف؟')
        self.current_results_mode='analysis'
        l.addWidget(tabs); return box

    def _build_analysis_tabs(self):
        tabs=QTabWidget(); hist=QWidget(); hl=QVBoxLayout(hist); self.hist=HistogramWidget(); hl.addWidget(self.hist)
        self.analysis_text=QTextEdit(); self.analysis_text.setReadOnly(True); hl.addWidget(self.analysis_text)
        tabs.addTab(hist,'الهستوغرام'); stats=QTextEdit(); stats.setReadOnly(True); self.stats_detail=stats; tabs.addTab(stats,'الإحصائيات'); return tabs

    def _build_log(self):
        box=QGroupBox('سجل العمليات'); l=QVBoxLayout(box); self.logbox=QTextEdit(); self.logbox.setReadOnly(True); l.addWidget(self.logbox); return box

    def _build_right_panel(self):
        panel=QWidget(); root=QVBoxLayout(panel); root.setContentsMargins(2,2,2,2)
        tabs=QTabWidget(); self.right_tabs=tabs; root.addWidget(tabs,1); panel.setMinimumWidth(355)
        tabs.addTab(self._workflow_panel(),'حالة جاهزية الصورتين للمقارنة')

        analysis_page=QScrollArea(); analysis_page.setWidgetResizable(True)
        inner=QWidget(); lay=QVBoxLayout(inner)
        r=QGroupBox('نتائج التحليل'); rl=QVBoxLayout(r)
        self.metric_similarity=MetricCard('درجة التشابه','—','#42df77'); self.metric_change=MetricCard('نسبة التغيير الحقيقي','—','#ffc13d'); self.metric_objects=MetricCard('التغييرات الحقيقية','—','#8bd5ff'); self.metric_stable=MetricCard('موجود في الصورتين','—','#42df77'); self.metric_appear=MetricCard('ظهور حقيقي','—','#ffad33'); self.metric_disappear=MetricCard('اختفاء حقيقي','—','#ff5a5a'); self.metric_structural=MetricCard('تغير بنيوي/موضعي','—','#4aa8ff'); self.metric_review=MetricCard('مراجعة / غير مؤكد','—','#aab6c5'); self.metric_regions=MetricCard('مرشحات Post-Filter','—','#58a6ff'); self.metric_area=MetricCard('مساحة التغيير الحقيقي','—','#e7edf7'); self.metric_mean=MetricCard('متوسط شدة التغيير','—','#ff9b4a'); self.metric_max=MetricCard('أعلى شدة تغيير','—','#ff5a5a')
        for m in (self.metric_similarity,self.metric_change,self.metric_objects,self.metric_stable,self.metric_appear,self.metric_disappear,self.metric_structural,self.metric_review,self.metric_regions,self.metric_area,self.metric_mean,self.metric_max): rl.addWidget(m)
        lay.addWidget(r)
        g=QGroupBox('التحكم في الكشف'); f=QFormLayout(g)
        self.method=QComboBox(); self.method.addItems(['ROBUST FINAL','ROBUST V6','ROBUST V5','ROBUST V4','ROBUST V3','ROBUST V2','ROBUST','NORMALIZED','SSIM','ABS','EDGE','STRUCTURAL']); self.method.setCurrentText('ROBUST FINAL'); self.method.setMinimumHeight(36); f.addRow('طريقة المقارنة',self.method)

        # Threshold: slider + directly editable numeric field, synchronized both ways.
        self.threshold=QSlider(Qt.Horizontal); self.threshold.setRange(1,255); self.threshold.setValue(35)
        self.threshold_spin=QSpinBox(); self._prepare_numeric_spin(self.threshold_spin, 1, 255, 35, step=1, width=112)
        self.threshold.valueChanged.connect(self.threshold_spin.setValue)
        self.threshold_spin.valueChanged.connect(self.threshold.setValue)
        self.threshold.valueChanged.connect(self._threshold_changed)
        tw=QWidget(); th=QHBoxLayout(tw); th.setContentsMargins(0,0,0,0); th.setSpacing(8); th.addWidget(self.threshold,1); th.addWidget(self.threshold_spin,0)
        f.addRow('Threshold',tw)

        self.min_area=QSpinBox(); self._prepare_numeric_spin(self.min_area, 1, 10_000_000, 100, step=10, width=180); f.addRow('أدنى مساحة (px)',self.min_area)
        self.kernel=QSpinBox(); self._prepare_numeric_spin(self.kernel, 1, 31, 3, step=2, width=180); f.addRow('Morphology',self.kernel)
        self.edge_tolerance=QSpinBox(); self._prepare_numeric_spin(self.edge_tolerance, 0, 12, 3, step=1, width=180); self.edge_tolerance.setToolTip('يستخدم مع ROBUST لقمع الحواف المتطابقة القريبة الناتجة عن خطأ مواءمة بسيط.'); f.addRow('Edge tolerance (px)',self.edge_tolerance)
        self.ignore_nodata_black=QCheckBox('تجاهل NoData والمناطق السوداء')
        self.ignore_nodata_black.setChecked(True)
        self.ignore_nodata_black.setToolTip('يستبعد تلقائيًا البكسلات السوداء/NoData غير المشتركة بين BEFORE وAFTER من المقارنة وكشف التغيير.')
        self.ignore_nodata_black.stateChanged.connect(self._invalidate_analysis_valid_mask)
        f.addRow(self.ignore_nodata_black)
        self.accept_conf=QSpinBox(); self._prepare_numeric_spin(self.accept_conf, 40, 95, 60, step=1, width=180); f.addRow('ثقة القبول %', self.accept_conf)
        self.review_conf=QSpinBox(); self._prepare_numeric_spin(self.review_conf, 20, 80, 45, step=1, width=180); f.addRow('ثقة المراجعة %', self.review_conf)
        self.merge_distance=QSpinBox(); self._prepare_numeric_spin(self.merge_distance, 0, 100, 12, step=1, width=180); self.merge_distance.setToolTip('مسافة تجميع الأجزاء المقبولة القريبة في مجموعة تغيير واحدة. لا توسّع خريطة التغيير؛ تستخدم للعد فقط.'); f.addRow('مسافة دمج الأجزاء (px)', self.merge_distance)
        self.align_ratio=QDoubleSpinBox(); self._prepare_numeric_spin(self.align_ratio, .50, .95, .72, step=.01, decimals=2, width=180); f.addRow('SIFT ratio',self.align_ratio)
        apply=QPushButton('تطبيق كشف التغيير'); apply.setObjectName('primary'); apply.clicked.connect(self.detect_changes); f.addRow(apply); lay.addWidget(g)
        quick=QGroupBox('المعالجة'); q=QVBoxLayout(quick)
        b1=QPushButton('1) مواءمة متقدمة تلقائية (SIFT + MAGSAC + ECC)'); b1.clicked.connect(self.align); q.addWidget(b1)
        b2=QPushButton('2) حساب المقارنة'); b2.clicked.connect(self.run_compare); q.addWidget(b2)
        b3=QPushButton('3) استخراج التغيير'); b3.clicked.connect(self.detect_changes); q.addWidget(b3)
        self._comparison_controls=[apply,b2,b3]
        b4=QPushButton('تصدير PNG / GeoTIFF / GeoJSON'); b4.clicked.connect(self.export_all); q.addWidget(b4); lay.addWidget(quick)
        lay.addStretch(1); analysis_page.setWidget(inner); tabs.addTab(analysis_page,'التحليل')

        # GIS Toolbox — works entirely inside the same Python 3.11 environment.
        gis_page=QScrollArea(); gis_page.setWidgetResizable(True)
        gis_inner=QWidget(); gis_lay=QVBoxLayout(gis_inner)
        gg=QGroupBox('GIS Toolbox — Python واحد فقط'); ggl=QVBoxLayout(gg)
        self.gis_status=QLabel('جاهز — Rasterio / PyProj / Shapely. لا يعتمد على ArcGIS أو QGIS Python.')
        self.gis_status.setWordWrap(True); self.gis_status.setStyleSheet('color:#6ee7b7;font-weight:700;padding:6px;')
        ggl.addWidget(self.gis_status)
        gb1=QPushButton('فحص بيانات الصورتين و CRS / Pixel Size / التداخل'); gb1.clicked.connect(self.gis_inspect_pair); ggl.addWidget(gb1)
        gb2=QPushButton('قص منطقة التداخل الجغرافي إلى ملفين GeoTIFF'); gb2.clicked.connect(self.gis_crop_overlap); ggl.addWidget(gb2)
        proc_row=QWidget(); proc_lay=QHBoxLayout(proc_row); proc_lay.setContentsMargins(0,0,0,0); proc_lay.setSpacing(8)
        proc_lay.addWidget(QLabel('معالج توحيد الصور:'))
        self.gis_processor=QComboBox(); self.gis_processor.addItem('CPU — جميع الأنوية','cpu'); self.gis_processor.addItem('GPU — NVIDIA CUDA','gpu'); self.gis_processor.setCurrentIndex(0); self.gis_processor.setMinimumHeight(36); self.gis_processor.setMinimumWidth(220)
        proc_lay.addWidget(self.gis_processor,1); ggl.addWidget(proc_row)
        gb3=QPushButton('توحيد الصورة 2 على شبكة الصورة 1 (CRS / Pixel / Extent)'); gb3.clicked.connect(self.gis_align_to_reference); ggl.addWidget(gb3)
        self.gis_inspect_colors_button=QPushButton('فحص الإضاءة والألوان')
        self.gis_inspect_colors_button.clicked.connect(self.gis_inspect_radiometry)
        ggl.addWidget(self.gis_inspect_colors_button)
        gb4=QPushButton('توحيد الإضاءة والألوان (Radiometric / Histogram Match)'); gb4.clicked.connect(self.gis_radiometric_match); ggl.addWidget(gb4)
        self.gis_radiometric_button=gb4
        gis_lay.addWidget(gg)
        gn=QLabel('هذه الأدوات تعمل من نفس Python الخاص بالمشروع. النتائج GeoTIFF كاملة الدقة على القرص، ولا تستخدم ArcPy أو Python خارجي.')
        gn.setWordWrap(True); gn.setStyleSheet('color:#9fb2c8;padding:8px;')
        gis_lay.addWidget(gn); gis_lay.addStretch(1)
        gis_page.setWidget(gis_inner)
        tabs.addTab(gis_page,'GIS Tools')

        # Session reports dashboard: keeps TXT, Excel and visual outputs visible to the user.
        report_page=QWidget(); rp=QVBoxLayout(report_page)
        rg=QGroupBox('التقارير المحفوظة لهذه الجلسة'); rgl=QVBoxLayout(rg)
        self.reports_text=QTextEdit(); self.reports_text.setReadOnly(True); self.reports_text.setMinimumHeight(250); rgl.addWidget(self.reports_text)
        rb=QGridLayout()
        b=QPushButton('🔄 تحديث القائمة'); b.clicked.connect(self.refresh_reports_panel); rb.addWidget(b,0,0)
        b=QPushButton('📂 فتح مجلد التقارير'); b.clicked.connect(self.open_reports_folder); rb.addWidget(b,0,1)
        b=QPushButton('📁 فتح مجلد نتائج الجلسة الحالية'); b.clicked.connect(self.open_current_results_folder); rb.addWidget(b,1,0,1,2)
        b=QPushButton('📗 فتح Excel'); b.clicked.connect(lambda:self._open_report_file('xlsx')); rb.addWidget(b,2,0)
        b=QPushButton('📝 فتح التقرير النصي'); b.clicked.connect(lambda:self._open_report_file('txt')); rb.addWidget(b,2,1)
        b=QPushButton('💾 حفظ تقرير الآن'); b.clicked.connect(self.save_report_now); rb.addWidget(b,3,0)
        b=QPushButton('⬆ تصدير النتائج'); b.clicked.connect(self.export_all); rb.addWidget(b,3,1)
        rgl.addLayout(rb); rp.addWidget(rg)
        note=QLabel('الحفظ تلقائي بعد كل عملية رئيسية. عند استخراج التغيير يُنشأ مجلد Analysis مستقل يحتوي BEFORE وAFTER وCHANGE MAP وJSON وملخص TXT ولقطة للواجهة.')
        note.setWordWrap(True); note.setStyleSheet('color:#9fb2c8;padding:6px;'); rp.addWidget(note); rp.addStretch(1)
        tabs.addTab(report_page,'التقارير')
        return panel

    def _prepare_numeric_spin(self, spin, minimum, maximum, value, step=1, decimals=None, width=170):
        """Make numeric controls large, directly editable and stable in an Arabic RTL interface."""
        spin.setRange(minimum, maximum)
        if decimals is not None and isinstance(spin, QDoubleSpinBox):
            spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setValue(value)
        spin.setMinimumWidth(width)
        spin.setMinimumHeight(38)
        spin.setKeyboardTracking(False)
        spin.setAccelerated(True)
        spin.setLayoutDirection(Qt.LeftToRight)
        spin.setAlignment(Qt.AlignCenter)
        spin.setButtonSymbols(QSpinBox.UpDownArrows)
        if spin.lineEdit() is not None:
            spin.lineEdit().setLayoutDirection(Qt.LeftToRight)
            spin.lineEdit().setAlignment(Qt.AlignCenter)
            spin.lineEdit().setMinimumHeight(32)
            spin.lineEdit().setStyleSheet('font-size:15px;font-weight:700;padding:2px 8px;')

    def _apply_style(self):
        self.setStyleSheet('''
        QMainWindow,QWidget{background:#0e141c;color:#e8edf5;font-family:"Segoe UI";font-size:12px;}
        QToolBar{background:#17212d;border:1px solid #273343;spacing:5px;padding:7px;} QToolBar QToolButton{background:#202d3b;border:1px solid #304157;padding:8px 10px;border-radius:5px;} QToolBar QToolButton:hover{background:#2a3b4e;}
        QGroupBox{border:1px solid #2b3949;border-radius:5px;margin-top:10px;padding-top:9px;font-weight:600;} QGroupBox::title{subcontrol-origin:margin;left:9px;padding:0 5px;color:#cbd6e5;}
        QPushButton{background:#1d2936;border:1px solid #304155;border-radius:5px;padding:7px;} QPushButton:hover{background:#28394b;} QPushButton#primary{background:#1676e8;color:white;font-weight:700;padding:10px;}
        QTextEdit,QTableWidget,QComboBox,QSpinBox,QDoubleSpinBox{background:#111923;border:1px solid #33465b;border-radius:5px;padding:5px;min-height:30px;} QSpinBox,QDoubleSpinBox{font-size:14px;font-weight:700;} QSpinBox::up-button,QDoubleSpinBox::up-button{width:24px;background:#1b2a3a;border-left:1px solid #33465b;border-bottom:1px solid #33465b;} QSpinBox::down-button,QDoubleSpinBox::down-button{width:24px;background:#1b2a3a;border-left:1px solid #33465b;border-top:1px solid #33465b;} QSpinBox::up-button:hover,QSpinBox::down-button:hover,QDoubleSpinBox::up-button:hover,QDoubleSpinBox::down-button:hover{background:#285079;} QProgressBar{background:#111923;border:1px solid #2b3949;border-radius:4px;text-align:center;min-height:22px;} QProgressBar::chunk{background:#1676e8;border-radius:3px;} QHeaderView::section{background:#1a2633;color:#b9c6d6;border:0;padding:5px;} QTableWidget{gridline-color:#243242;}
        QSlider::groove:horizontal{height:5px;background:#263546;border-radius:2px;} QSlider::handle:horizontal{width:14px;margin:-5px 0;background:#dce8f7;border-radius:7px;} QSlider::sub-page:horizontal{background:#2687ef;}
        QStatusBar{background:#121b25;border-top:1px solid #293747;} QLabel#viewerFooter{background:#17212b;color:#98a9bc;padding:5px;} QFrame#metricCard{background:#182330;border:1px solid #2b3a4b;border-radius:5px;}
        QTabWidget::pane{border:1px solid #2b3949;} QTabBar::tab{background:#17212d;padding:6px 12px;} QTabBar::tab:selected{background:#263647;}
        ''')

    def _image_report_info(self, data):
        if data is None:
            return {}
        try:
            m = engine.image_metadata(data)
            return {'path': data.path, 'width': m.get('width'), 'height': m.get('height'), 'crs': m.get('crs'), 'pixel_size': m.get('pixel_size')}
        except Exception:
            return {'path': getattr(data, 'path', '')}

    def _report_snapshot(self, report_kind='analysis'):
        if report_kind == 'arcgis':
            settings = {
                'arcgis_difference_mode': self.arcgis_diff_mode.currentText() if hasattr(self, 'arcgis_diff_mode') else '',
                'arcgis_threshold': self.arcgis_threshold.value() if hasattr(self, 'arcgis_threshold') else '',
                'min_area_px': self.min_area.value() if hasattr(self, 'min_area') else '',
                'arcgis_boundary_mode': self.boundary_combo.currentText() if hasattr(self, 'boundary_combo') else 'مضلعات',
                'full_resolution': True,
            }
            return {
                'image1': self._image_report_info(self.a),
                'image2': self._image_report_info(self.b),
                'settings': settings,
                'alignment': dict(self.last_arcgis_alignment_report or {}),
                'comparison': {},
                'change_summary': dict(self.last_arcgis_change_stats or {}),
                'regions': [dict(r) for r in (self.arcgis_regions or [])],
                'objects': [],
            }

        settings = {
            'method': self.method.currentText() if hasattr(self, 'method') else '',
            'threshold': self.threshold.value() if hasattr(self, 'threshold') else '',
            'min_area_px': self.min_area.value() if hasattr(self, 'min_area') else '',
            'morphology': self.kernel.value() if hasattr(self, 'kernel') else '',
            'edge_tolerance_px': self.edge_tolerance.value() if hasattr(self, 'edge_tolerance') else '',
            'accept_confidence_pct': self.accept_conf.value() if hasattr(self, 'accept_conf') else '',
            'review_confidence_pct': self.review_conf.value() if hasattr(self, 'review_conf') else '',
            'merge_distance_px': self.merge_distance.value() if hasattr(self, 'merge_distance') else '',
            'overlay_opacity_pct': self.opacity.value() if hasattr(self, 'opacity') else '',
            'sync_views': self._sync_enabled() if hasattr(self,'sync_action') else False,
            'sync_master': 'Any viewport / geospatial coordinates',
            'readiness': dict(self._wf_state),
            'preparation': self.workflow_project_state(),
            'radiometric_inspection': self._wf_radiometric,
        }
        change = dict(self.last_change_stats or {})
        if self.compare_meta:
            change.setdefault('similarity_percent', self.compare_meta.get('similarity_percent'))
        change.setdefault('object_count', len(self.change_objects or []))
        change.setdefault('accepted_count', len([r for r in (self.regions or []) if r.get('classification','تغير مرجح') == 'تغير مرجح']))
        change.setdefault('appearance_count', sum(1 for o in (self.change_objects or []) if o.get('change_type') == 'ظهور محتمل'))
        change.setdefault('disappearance_count', sum(1 for o in (self.change_objects or []) if o.get('change_type') == 'اختفاء محتمل'))
        change.setdefault('structural_count', sum(1 for o in (self.change_objects or []) if o.get('change_type') == 'تغير بنيوي/موضعي'))
        change.setdefault('radiometric_count', sum(1 for o in (self.change_objects or []) if o.get('change_type') == 'تغير مظهري/سطوع'))
        return {
            'image1': self._image_report_info(self.a),
            'image2': self._image_report_info(self.b),
            'settings': settings,
            'alignment': dict(self.last_alignment_report or {}),
            'comparison': dict(self.compare_meta or {}),
            'change_summary': change,
            'regions': [dict(r) for r in (self.regions or [])],
            'objects': [dict(o) for o in (self.change_objects or [])],
            'region_columns': [dict(c) for c in self.report_columns],
            'report_metadata': dict(self.report_metadata),
        }

    def _save_report(self, operation, status='نجاح', details=''):
        saved=False
        try:
            target = self.reporter
            if target is not None:
                target.record(operation, status, details, self._report_snapshot('analysis'))
                self.refresh_reports_panel()
                saved=True
        except Exception as exc:
            self.statusBar().showMessage('تعذر تحديث التقرير: ' + str(exc), 7000)
        return saved

    def save_report_now(self):
        if not self.region_autosave.store and self.regions:
            self._save_analysis_tables_excel(None)
        self.region_autosave.flush()
        if self.region_autosave.dirty:
            self._save_report('حفظ تقرير يدوي','نجاح','تحديث التقرير؛ حفظ جدول المناطق جارٍ أو يحتاج معالجة الخطأ الظاهر.')
            return
        if self._save_report('حفظ تقرير يدوي', 'نجاح', 'تم تحديث ملفات TXT وExcel يدويًا.'):
            QMessageBox.information(self, 'التقارير', 'تم تحديث التقرير النصي وملف Excel بنجاح.\n' + str(self.reporter.session_dir))
        else:
            QMessageBox.warning(self,'تعذر الحفظ','تعذر حفظ التقرير. تحقق من القرص وصلاحية الكتابة والمساحة المتاحة، ثم اختر مسارًا بديلًا من إدارة التخزين.')

    def open_report_builder(self):
        dialog = ReportBuilderDialog(self._report_snapshot('analysis'), self)
        dialog.exec()

    def open_current_results_folder(self):
        """V40: open the report folder that matches the currently displayed result source."""
        try:
            reporter=self.reporter
            folder=str(getattr(reporter,'session_dir','') or '')
            if not folder:
                raise RuntimeError('لا يوجد مجلد جلسة حالي.')
            os.makedirs(folder,exist_ok=True)
            if sys.platform.startswith('win'):
                os.startfile(folder)
            elif sys.platform == 'darwin':
                subprocess.Popen(['open',folder])
            else:
                subprocess.Popen(['xdg-open',folder])
            self.log('تم فتح مجلد نتائج الجلسة الحالية: '+folder)
        except Exception as e:
            self.err(e)

    def open_reports_folder(self):
        try:
            self.reporter.open_folder()
        except Exception as e:
            self.err(e)

    def _open_report_file(self, kind='xlsx'):
        try:
            if not getattr(self,'reporter',None):
                return
            path = str(self.reporter.xlsx_path if kind == 'xlsx' else self.reporter.txt_path)
            if not os.path.isfile(path):
                raise RuntimeError('ملف التقرير غير موجود بعد: ' + path)
            if os.name == 'nt':
                os.startfile(path)
            else:
                subprocess.Popen(['xdg-open', path])
        except Exception as e:
            self.err(e)

    def refresh_reports_panel(self):
        if not hasattr(self,'reports_text'):
            return
        try:
            sections = []
            for title, reporter in (
                ('جلسة التحليل', getattr(self, 'reporter', None)),
            ):
                if reporter is None:
                    continue
                base = str(reporter.session_dir)
                sections += [title + ':', base, '', 'الملفات المحفوظة:']
                files=[]
                for root,dirs,names in os.walk(base):
                    dirs.sort(reverse=True); names.sort(reverse=True)
                    for n in names:
                        p=os.path.join(root,n)
                        rel=os.path.relpath(p,base)
                        try:
                            kb=os.path.getsize(p)/1024.0
                        except Exception:
                            kb=0
                        files.append((os.path.getmtime(p) if os.path.exists(p) else 0, rel, kb))
                files.sort(reverse=True)
                for _,rel,kb in files[:30]:
                    sections.append(f'• {rel}   ({kb:,.1f} KB)')
                if not files:
                    sections.append('• لا توجد ملفات بعد.')
                sections += ['', '=' * 72, '']
            self.reports_text.setPlainText('\n'.join(sections))
        except Exception as exc:
            self.reports_text.setPlainText('تعذر قراءة مجلد التقارير: '+str(exc))

    def _cleanup_stale_temp_files(self, older_than_hours=6):
        """Remove only ChangeVision-owned stale TEMP leftovers from crashed/old runs."""
        try:
            root = tempfile.gettempdir()
            cutoff = time.time() - float(older_than_hours) * 3600.0
            patterns = ('changevision_arcgis_*', 'cv_side_*.png')
            for pattern in patterns:
                for p in glob.glob(os.path.join(root, pattern)):
                    try:
                        if os.path.getmtime(p) > cutoff:
                            continue
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        elif os.path.isfile(p):
                            os.remove(p)
                    except Exception:
                        pass
        except Exception:
            pass

    def _release_ram(self):
        """Release Python/OpenCV temporaries without touching active analysis results."""
        try:
            gc.collect()
        except Exception:
            pass
        try:
            if hasattr(cv2, 'ocl') and hasattr(cv2.ocl, 'finish'):
                cv2.ocl.finish()
        except Exception:
            pass

    def _add_analysis_geographic_coordinates(self):
        """Attach WGS84 Longitude/Latitude to normal-analysis regions from GeoTIFF georeferencing."""
        if not self.regions or self.a is None or getattr(self.a, 'transform', None) is None or getattr(self.a, 'crs', None) is None:
            return
        try:
            xs=[]; ys=[]; idx=[]
            tr=self.a.transform
            for i,row in enumerate(self.regions):
                cx=row.get('cx'); cy=row.get('cy')
                if cx is None or cy is None:
                    continue
                # Region centroid is in full-resolution pixel coordinates.
                x_geo, y_geo = tr * (float(cx) + 0.5, float(cy) + 0.5)
                xs.append(float(x_geo)); ys.append(float(y_geo)); idx.append(i)
            if not idx:
                return
            crs_text=str(self.a.crs)
            if crs_text.upper() not in ('EPSG:4326','OGC:CRS84'):
                from rasterio.warp import transform as rio_transform
                xs, ys = rio_transform(self.a.crs, 'EPSG:4326', xs, ys)
            for i,lon,lat in zip(idx,xs,ys):
                self.regions[i]['longitude']=float(lon)
                self.regions[i]['latitude']=float(lat)
        except Exception as exc:
            self.log('تحذير: تعذر حساب Longitude/Latitude للتحليل: '+str(exc))

    def log(self,msg):
        t=datetime.datetime.now().strftime('%H:%M:%S'); self.logbox.append(f'✓ {t}  {msg}'); self.statusBar().showMessage(msg,5000)

    def err(self,e): QMessageBox.critical(self,'خطأ',str(e)); self.log('خطأ: '+str(e))
    def need(self):
        if self.a is None or self.b is None: raise RuntimeError('اختر الصورتين أولًا.')
    def current_b(self): return self.aligned if self.aligned is not None else self.b.array

    def _invalidate_analysis_valid_mask(self, *_args):
        self.analysis_valid_mask = None

    def _effective_analysis_valid_mask(self):
        """Return overlap mask intersected with common non-black image data when enabled."""
        if not hasattr(self, 'ignore_nodata_black') or not self.ignore_nodata_black.isChecked():
            return self.valid_mask
        if self.analysis_valid_mask is None:
            self.analysis_valid_mask = engine.common_data_valid_mask(
                self.a.array, self.current_b(), base_valid_mask=self.valid_mask,
                black_threshold=2, rows_per_chunk=512
            )
            valid_px = int(np.count_nonzero(self.analysis_valid_mask))
            total_px = int(self.analysis_valid_mask.size)
            pct = 100.0 * valid_px / max(total_px, 1)
            self.log(f'حماية NoData/الأسود مفعلة: التحليل سيستخدم {pct:.2f}% من البكسلات المشتركة الصالحة فقط.')
        return self.analysis_valid_mask

    def open_a(self):
        p,_=QFileDialog.getOpenFileName(self,'فتح الصورة الأولى','','All files (*);;Images (*)')
        if p:
            try: self._load_a(p)
            except Exception as e:self.err(e)
    def _load_a(self,p):
        if not self._excel_can_leave():raise RuntimeError('انتظر حفظ تعديلات Excel قبل تغيير الصور.')
        loaded=read_analysis_image(p)
        self.region_autosave.bind(None)
        self._workflow_before_load(0,p)
        self._invalidate_registration()
        self._gis_overlap_reference_path = None; self._gis_overlap_moving_path = None
        self.analysis_valid_mask = None
        self.a=loaded; self.va.set_array(self.a.array); self.status_img.setText(f'الصورة: {self.a.width}×{self.a.height}'); self._show_meta(); self.log('تم فتح الصورة الأولى: '+p); self._save_report('فتح الصورة الأولى', 'نجاح', p)
        self._workflow_after_load(0)
    def open_b(self):
        p,_=QFileDialog.getOpenFileName(self,'فتح الصورة الثانية','','All files (*);;Images (*)')
        if p:
            try:self._load_b(p)
            except Exception as e:self.err(e)
    def _load_b(self,p):
        if not self._excel_can_leave():raise RuntimeError('انتظر حفظ تعديلات Excel قبل تغيير الصور.')
        loaded=read_analysis_image(p)
        self.region_autosave.bind(None)
        self._workflow_before_load(1,p)
        self._invalidate_registration()
        self._gis_overlap_reference_path = None; self._gis_overlap_moving_path = None
        self.analysis_valid_mask = None
        self.b=loaded; self.vb.set_array(self.b.array); self.aligned=None; self.valid_mask=None; self.log('تم فتح الصورة الثانية: '+p); self._save_report('فتح الصورة الثانية', 'نجاح', p)
        self._workflow_after_load(1)

    def _show_meta(self):
        if not self.a:return
        m=engine.image_metadata(self.a); self.meta_size.setText(f"{m['width']} × {m['height']}"); self.meta_crs.setText(m['crs']); self.meta_px.setText(m['pixel_size']); self.status_crs.setText('CRS: '+m['crs'])

    def align(self):
        try:
            self.need()
            self._workflow_assert_idle();self._workflow_alignment_gate();self._remember_step()
            self._wf_radiometric=None;self._wf_rad_accepted=False;self._wf_stage='المواءمة الهندسية'
            if self._worker_running(self._align_worker):
                self.log('المواءمة المتقدمة تعمل بالفعل...')
                return
            self._invalidate_registration()
            self.task_progress.setValue(0); self.task_progress.setVisible(True)
            self.statusBar().showMessage('بدء المواءمة المتقدمة...')
            self.log('بدء المواءمة المتقدمة: SIFT → Auto Ratio → MAGSAC → Homography → ECC → Local Residual → Smooth Local Mesh → Quality.')
            self._align_worker = AlignWorker(self.a.array, self.b.array, self.align_ratio.value(), self)
            self._align_worker.progress.connect(self._align_progress)
            self._align_worker.completed.connect(self._align_done)
            self._align_worker.failed.connect(self._align_failed)
            self._align_worker.finished.connect(self._align_worker_finished)
            self._align_worker.start()
            self._refresh_readiness()
        except Exception as e:
            self.err(e)

    def _align_progress(self, value, text):
        self.task_progress.setRange(0,100)
        self.task_progress.setValue(int(value))
        self.task_progress.setFormat(f'{int(value)}%')
        self.statusBar().showMessage(text)
        self._wf_stage=text;self.workflow_current.setText('الخطوة الحالية: '+text)

    def _align_done(self, aligned, H, report, valid_mask):
        report.update(shift_x_px=float(H[0,2]),shift_y_px=float(H[1,2]),
                      rotation_deg=float(np.degrees(np.arctan2(H[1,0],H[0,0]))),
                      scale=float(np.hypot(H[0,0],H[1,0])))
        self.aligned = aligned; self.H = H; self.valid_mask = valid_mask; self.analysis_valid_mask = None
        self.last_alignment_report = dict(report or {})
        self._wf_stage='اكتملت المواءمة وفحص الجودة';self._refresh_readiness()
        self.vb.set_array(self.aligned)
        self.diff = None; self.mask = None; self.regions = []
        if self._pending_project_analysis is not None:
            self._restore_project_analysis(self._pending_project_analysis)
            self._pending_project_analysis = None
        ratio = report.get('inlier_ratio', 0) * 100.0
        ecc = report.get('ecc_score')
        ecc_text = f'{ecc:.4f}' if ecc is not None else 'لم يستخدم'
        txt = (
            f"جودة المواءمة: {report.get('quality_ar','—')} ({report.get('quality_score',0):.1f}/100)\n"
            f"SIFT ratio المختار تلقائيًا: {report.get('selected_ratio',0):.2f}\n"
            f"Matches: {report.get('matches',0)}\n"
            f"Inliers: {report.get('inliers',0)} ({ratio:.2f}%)\n"
            f"Reprojection Error: {report.get('reprojection_error_px',0):.2f} px\n"
            f"Inlier Coverage: {report.get('coverage_percent',0):.2f}%\n"
            f"Valid Overlap: {report.get('overlap_percent',0):.2f}%\n"
            f"ECC: {ecc_text} | refined={report.get('ecc_used',False)}\n"
            f"Residual median: {report.get('residual_median_px',float('nan')):.2f} px\n"
            f"Residual P90: {report.get('residual_p90_px',0):.2f} px\n"
            f"Local Residual passes: {report.get('local_affine_passes',0)}\n"
            f"Smooth Local Mesh: {report.get('smooth_mesh_passes',0)} pass(es) | points={report.get('smooth_mesh_points',0)}"
        )
        self.analysis_text.setPlainText(txt)
        # Use the measured reprojection error to initialize edge uncertainty tolerance.
        # The user can still override it manually after alignment.
        reproj_err = float(report.get('reprojection_error_px', 0) or 0)
        auto_tol = int(np.clip(np.ceil(reproj_err * 1.35), 2, 8)) if reproj_err > 0 else self.edge_tolerance.value()
        self.edge_tolerance.setValue(auto_tol)
        self.analysis_text.append(f'\nEdge tolerance auto: {auto_tol} px (from reprojection error)')
        self.task_progress.setValue(100); self.statusBar().showMessage('اكتملت المواءمة المتقدمة.', 6000)
        self.log(f"اكتملت المواءمة المتقدمة: الجودة={report.get('quality_ar')}، matches={report.get('matches')}، inliers={report.get('inliers')} ({ratio:.1f}%)، error={report.get('reprojection_error_px',0):.2f}px، coverage={report.get('coverage_percent',0):.1f}%.")
        self.task_progress.setVisible(False)
        self.refresh_result_view()
        self._save_report('المواءمة المتقدمة', 'نجاح', f"الجودة={report.get('quality_ar','—')} | error={report.get('reprojection_error_px',0):.2f}px")

    def _align_failed(self, text):
        self._wf_sequence=False
        self.aligned = self.H = self.valid_mask = self.diff = self.mask = None
        self.last_alignment_report = {}
        self.task_progress.setVisible(False)
        self.statusBar().showMessage('فشلت المواءمة المتقدمة.', 6000)
        self._save_report('المواءمة المتقدمة', 'فشل', text)
        if not text.startswith('تم إيقاف'):self.err(text)
        self._wf_stage='فشلت أو أوقفت المواءمة';self._refresh_readiness()

    def _worker_running(self, worker):
        """Return QThread running state without touching an already-deleted Shiboken wrapper."""
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def _dispose_worker(self, attr_name):
        """Clear the Python reference before scheduling the finished QThread for deletion."""
        worker = getattr(self, attr_name, None)
        setattr(self, attr_name, None)
        if worker is not None:
            try:
                worker.deleteLater()
            except RuntimeError:
                pass
        self._release_ram()
        self._refresh_readiness()
        if self._wf_sequence:QTimer.singleShot(0,self._workflow_sequence_continue)

    def _align_worker_finished(self):
        self._dispose_worker('_align_worker')

    def _compare_worker_finished(self):
        self._dispose_worker('_compare_worker')

    def _change_worker_finished(self):
        self._dispose_worker('_change_worker')

    def _invalidate_registration(self):
        for name in ('_align_worker', '_compare_worker', '_change_worker'):
            if self._worker_running(getattr(self, name, None)):
                raise RuntimeError('انتظر انتهاء العملية الحالية قبل تغيير الصور أو المواءمة.')
        self.aligned = self.H = self.valid_mask = self.analysis_valid_mask = None
        self.diff = self.mask = None
        self.regions = []; self.change_objects = []
        self.last_alignment_report = {}

    def _require_accurate_alignment(self):
        if (self.aligned is None or not self.last_alignment_report.get('alignment_approved')
                or self._worker_running(self._align_worker)):
            raise RuntimeError('يلزم تنفيذ مواءمة دقيقة ناجحة قبل كشف التغير أو المقارنة.')

    def run_compare(self):
        try:
            self.need()
            self._require_accurate_alignment()
            self._workflow_assert_idle();self._workflow_compare_gate();self._remember_step();self._wf_stage='حساب المقارنة'
            if self._worker_running(self._compare_worker):
                self.log('المقارنة تعمل بالفعل...')
                return
            if self._worker_running(self._change_worker):
                self.log('انتظر انتهاء استخراج التغيير أولًا.')
                return
            method = self.method.currentText()
            self._release_ram()
            self.task_progress.setValue(0); self.task_progress.setVisible(True)
            self.statusBar().showMessage(f'جاري حساب المقارنة بطريقة {method}...')
            self.log(f'بدء المقارنة بطريقة {method}.')
            effective_valid_mask = self._effective_analysis_valid_mask()
            self._compare_worker = CompareWorker(
                self.a.array, self.current_b(), method, effective_valid_mask,
                self.edge_tolerance.value(), self
            )
            self._compare_worker.progress.connect(self._analysis_progress)
            self._compare_worker.completed.connect(self._compare_done)
            self._compare_worker.failed.connect(self._analysis_failed)
            self._compare_worker.finished.connect(self._compare_worker_finished)
            self._compare_worker.start()
            self._refresh_readiness()
        except Exception as e:
            self.err(e)

    def _analysis_progress(self, value, text):
        self.task_progress.setValue(int(value)); self.task_progress.setFormat(f'{int(value)}%')
        self.statusBar().showMessage(text)

    def _compare_done(self, diff, meta):
        self.diff = diff; self.compare_meta = dict(meta or {})
        self._wf_stage='اكتملت المقارنة';self._refresh_readiness()
        method = self.method.currentText()
        self.metric_similarity.t.setText('درجة التشابه' if method != 'SSIM' else 'نسبة التشابه (SSIM)')
        self.metric_similarity.set_value(f"{self.compare_meta.get('similarity_percent',0):.2f}%")
        h,_ = engine.histogram(self.diff); self.hist.set_histogram(h)
        if method in ('ROBUST FINAL','ROBUST V6','ROBUST V5','ROBUST V4','ROBUST V3','ROBUST V2','ROBUST'):
            ver = 'FINAL (V6 + Post-Filter)' if method == 'ROBUST FINAL' else ('V6' if method == 'ROBUST V6' else ('V5' if method == 'ROBUST V5' else ('V4' if method == 'ROBUST V4' else ('V3' if method == 'ROBUST V3' else ('V2' if method == 'ROBUST V2' else 'V1')))))
            self.analysis_text.append(
                f"\nROBUST Change Detection {ver}\nRadiometric normalization: ON\n"
                f"Confidence-aware edge suppression: ON\nLocal structural correlation: {'ON' if method in ('ROBUST V6','ROBUST V5','ROBUST V4','ROBUST V3') else 'OFF'}\nComponent confidence filter: {'ON' if method in ('ROBUST V6','ROBUST V5','ROBUST V4','ROBUST V3') else 'OFF'}\nObject core/thickness filter: {'ON' if method in ('ROBUST V6','ROBUST V5','ROBUST V4') else 'OFF'}\nAdaptive noise floor: {'ON' if method in ('ROBUST V6','ROBUST V5','ROBUST V4') else 'OFF'}\nHysteresis + compactness filter: {'ON' if method in ('ROBUST V6','ROBUST V5') else 'OFF'}\nRegion-first object filter: {'ON' if method == 'ROBUST V6' else 'OFF'}\nCoarse appearance support: {'ON' if method == 'ROBUST V6' else 'OFF'}\nEdge tolerance: {self.edge_tolerance.value()} px"
            )
        if hasattr(self, 'ignore_nodata_black') and self.ignore_nodata_black.isChecked():
            self.analysis_text.append('\nNoData / black-border exclusion: ON')
        self.task_progress.setValue(100); self.task_progress.setVisible(False)
        # Show verified polygons on BEFORE and AFTER too, so disappearance/appearance can be visually checked.
        if self.change_objects:
            self.va.set_array(self._annotate_presence_image(self.a.array, 'before'))
            self.vb.set_array(self._annotate_presence_image(self.current_b(), 'after'))
        self.refresh_result_view(); self.log(f'تمت المقارنة بطريقة {method}.')
        self._save_report('حساب المقارنة', 'نجاح', f"الطريقة={method} | التشابه={self.compare_meta.get('similarity_percent',0):.2f}%")

    def detect_changes(self):
        if not self._excel_can_leave():return
        try:
            self.need()
            self._require_accurate_alignment()
            self._workflow_assert_idle();self._workflow_compare_gate()
            if self.diff is None:
                self.run_compare()
                self.log('بعد انتهاء المقارنة اضغط استخراج التغيير مرة أخرى.')
                return
            if self._worker_running(self._change_worker):
                self.log('استخراج التغيير يعمل بالفعل...')
                return
            self.task_progress.setValue(0); self.task_progress.setVisible(True)
            self.statusBar().showMessage('جاري استخراج مناطق التغيير...')
            self._remember_step();self._wf_stage='استخراج التغيير'
            effective_valid_mask = self._effective_analysis_valid_mask()
            self._change_worker = ChangeWorker(
                self.diff, self.threshold.value(), self.min_area.value(), self.kernel.value(),
                effective_valid_mask, self.a, self.method.currentText(), self.edge_tolerance.value(),
                reference=self.a.array, moving=self.current_b(),
                accept_confidence=self.accept_conf.value(), review_confidence=self.review_conf.value(),
                merge_distance=self.merge_distance.value(), parent=self
            )
            self._change_worker.progress.connect(self._analysis_progress)
            self._change_worker.completed.connect(self._change_done)
            self._change_worker.failed.connect(self._analysis_failed)
            self._change_worker.finished.connect(self._change_worker_finished)
            self._change_worker.start()
            self._refresh_readiness()
        except Exception as e:
            self.err(e)

    def _change_done(self, mask, regions, st):
        self._wf_stage='نتائج كشف التغيير';self._refresh_readiness()
        self._arcgis_change_active = False
        self._set_shared_results_mode('analysis')
        self.mask = mask; self.regions = ensure_target_metadata(list(regions)); st = dict(st)
        self._add_analysis_geographic_coordinates()
        self.change_objects = list(st.pop('change_objects', []))
        self.last_change_stats = dict(st)
        self.last_change_stats['object_count'] = int(st.get('object_count', len(self.change_objects)))
        self.metric_change.set_value(f"{st['changed_percent']:.2f}%")
        self.metric_objects.set_value(str(st.get('verified_change_count', sum(1 for o in self.change_objects if o.get('is_real_change',False)))))
        self.metric_appear.set_value(str(st.get('appearance_count',0))); self.metric_stable.set_value(str(st.get('stable_count',0)))
        self.metric_disappear.set_value(str(st.get('disappearance_count',0))); self.metric_review.set_value(str(st.get('verification_review_count',0)))
        self.metric_structural.set_value(str(st.get('structural_count', sum(1 for o in self.change_objects if o.get('change_type')=='تغير بنيوي/موضعي'))))
        self.metric_regions.set_value(str(st.get('accepted_count', len([r for r in self.regions if r.get('classification','تغير مرجح') == 'تغير مرجح']))))
        area=st.get('changed_area_map_units2'); self.metric_area.set_value(f'{area:,.2f}' if area is not None else f"{st['changed_pixels']:,} px")
        self.metric_mean.set_value(f"{st.get('mean_change_intensity',0):.3f}"); self.metric_max.set_value(f"{st.get('max_change_intensity',0):.3f}")
        self._fill_regions(); self._fill_objects(); self.stats_detail.setPlainText('\n'.join(f'{k}: {v}' for k,v in st.items()))
        if 'candidate_count' in st:
            self.log(f"Post-Filter: المرشحون={st['candidate_count']} | مقبول={st['accepted_count']} | مراجعة={st['review_count']} | مرفوض={st['rejected_count']}")
            self.log(f"العد المكاني: مجموعات التغيير={st.get('object_count',0)} من المناطق المقبولة={st.get('accepted_region_count',st['accepted_count'])} | مسافة الدمج={st.get('merge_distance_px',0)} px")
            self.log(f"التحقق الثنائي: موجود في الصورتين={st.get('stable_count',0)} | ظل/حافة مستبعدة={st.get('protected_fragment_count',0)} | اختفاء حقيقي={st.get('disappearance_count',0)} | ظهور حقيقي={st.get('appearance_count',0)} | تغير بنيوي={st.get('structural_count',0)} | مراجعة={st.get('verification_review_count',0)}")
        self.task_progress.setValue(100); self.task_progress.setVisible(False)
        # Show verified polygons on BEFORE and AFTER too, so disappearance/appearance can be visually checked.
        if self.change_objects:
            self.va.set_array(self._annotate_presence_image(self.a.array, 'before'))
            self.vb.set_array(self._annotate_presence_image(self.current_b(), 'after'))
        self.refresh_result_view(); self.log('تم استخراج مناطق التغيير وتنظيفها وحساب الإحصائيات.')
        self._save_report('استخراج التغيير', 'نجاح', f"التغيير={st.get('changed_percent',0):.2f}% | المجموعات={st.get('object_count',len(self.change_objects))} | المقبول={st.get('accepted_count',0)}")
        self._auto_save_verified_outputs()

    def _table_as_excel_rows(self, table):
        """Return the exact visible table order (headers + rows) for Excel export."""
        headers=[]
        for c in range(table.columnCount()):
            item=table.horizontalHeaderItem(c)
            headers.append(item.text() if item is not None else f'Column {c+1}')
        rows=[headers]
        for r in range(table.rowCount()):
            vals=[]
            for c in range(table.columnCount()):
                item=table.item(r,c)
                if item is not None:
                    vals.append(item.text())
                    continue
                widget=table.cellWidget(r,c)
                if widget is not None and hasattr(widget,'text'):
                    try:
                        vals.append(widget.text())
                        continue
                    except Exception:
                        pass
                vals.append('')
            rows.append(vals)
        return rows

    def _region_excel_rows(self, include_all=False):
        """Build the report sheet from stable column keys, not editable titles."""
        ensure_target_metadata(self.regions)
        columns=[column for column in self.report_columns if column.get('visible',True)]
        if not columns:
            columns=[dict(column) for column in DEFAULT_COLUMNS]
            self.report_columns=columns
        rows=[[column.get('title',column['key']) for column in columns]]
        for target in self.regions:
            if not include_all and not target.get('report_included',False):
                continue
            values=[]
            for column in columns:
                value=value_for_column(target,column)
                if value in ('',None) and column.get('key') in self.report_metadata:
                    value=self.report_metadata[column['key']]
                values.append(value)
            rows.append(values)
        return rows

    def manage_targets(self):
        dialog=TargetManagerDialog(self.regions,self)
        if dialog.exec()==QDialog.Accepted:
            self.regions=dialog.regions
            self._fill_regions()
            self._queue_region_excel()
            self._save_report('إدارة الأهداف','نجاح',f'عدد الأهداف: {len(self.regions)}')

    def add_selected_targets_to_report(self):
        """Choose an image source separately for each selected target and preview it."""
        selected_rows={index.row() for index in self.table.selectionModel().selectedRows()}
        rows=[row for row in self._target_selection_order if row in selected_rows]
        rows.extend(sorted(selected_rows-set(rows)))
        if not rows and self.table.currentRow() >= 0: rows=[self.table.currentRow()]
        if not rows:
            QMessageBox.information(self, "\u0625\u0636\u0627\u0641\u0629 \u0635\u0648\u0631 \u0644\u0644\u062a\u0642\u0631\u064a\u0631", "\u062d\u062f\u062f \u0635\u0641\u0627\u064b \u0648\u0627\u062d\u062f\u0627\u064b \u0623\u0648 \u0623\u0643\u062b\u0631 \u0623\u0648\u0644\u0627\u064b.")
            return
        dialog=QDialog(self); dialog.setWindowTitle("\u0627\u062e\u062a\u064a\u0627\u0631 \u0635\u0648\u0631 \u0627\u0644\u0623\u0647\u062f\u0627\u0641 \u0644\u0644\u062a\u0642\u0631\u064a\u0631"); dialog.resize(720,650)
        layout=QVBoxLayout(dialog)
        layout.addWidget(QLabel(f"\u0627\u062e\u062a\u0631 \u0645\u0635\u062f\u0631\u0627\u064b \u0645\u0633\u062a\u0642\u0644\u0627\u064b \u0644\u0643\u0644 \u0647\u062f\u0641. \u0631\u0627\u062c\u0639 \u0627\u0644\u0645\u0639\u0627\u064a\u0646\u0629 \u0642\u0628\u0644 \u0627\u0644\u0625\u0636\u0627\u0641\u0629. ({len(rows)} \u0645\u062d\u062f\u062f)"))
        choices=QTableWidget(len(rows),3,dialog); choices.setHorizontalHeaderLabels(["ID","\u0627\u0633\u0645 \u0627\u0644\u0647\u062f\u0641","\u0645\u0635\u062f\u0631 \u0627\u0644\u0635\u0648\u0631\u0629"])
        choices.setSelectionBehavior(QTableWidget.SelectRows); choices.setSelectionMode(QTableWidget.SingleSelection)
        choices.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        roles=[("before","BEFORE \u2013 \u0635\u0648\u0631\u0629 \u0642\u0628\u0644"),("after","AFTER \u2013 \u0635\u0648\u0631\u0629 \u0628\u0639\u062f"),("change","CHANGE MAP \u2013 \u062e\u0631\u064a\u0637\u0629 \u0627\u0644\u062a\u063a\u064a\u064a\u0631")]
        role_boxes=[]
        for table_row,target_row in enumerate(rows):
            target=self.regions[target_row]
            choices.setItem(table_row,0,QTableWidgetItem(str(target.get("id",target_row+1))))
            choices.setItem(table_row,1,QTableWidgetItem(str(target.get("target_name") or target.get("title") or "")))
            box=QComboBox(choices)
            for role,label in roles: box.addItem(label,role)
            choices.setCellWidget(table_row,2,box);role_boxes.append(box)
        layout.addWidget(choices)
        preview_caption=QLabel();layout.addWidget(preview_caption)
        preview=QLabel();preview.setAlignment(Qt.AlignCenter);preview.setMinimumSize(600,300)
        preview.setStyleSheet("background:#111923;border:1px solid #33465b;color:#aab6c5;");layout.addWidget(preview,1)
        def make_crop(target,role):
            arr=(self.a.array if self.a is not None else None) if role in ("before","change") else (self.current_b() if self.b is not None else None)
            if arr is None:return None
            h,w=arr.shape[:2];x=int(target.get("x",target.get("cx",0)));y=int(target.get("y",target.get("cy",0)))
            bw=max(1,int(target.get("width",1)));bh=max(1,int(target.get("height",1)))
            if "x" not in target and "cx" in target:x-=bw//2
            if "y" not in target and "cy" in target:y-=bh//2
            margin=max(24,int(max(bw,bh)*1.5));x0=max(0,x-margin);y0=max(0,y-margin);x1=min(w,x+bw+margin);y1=min(h,y+bh+margin)
            if x1<=x0 or y1<=y0:return None
            if role=="change":return self._native_detail_crop("change",x0,y0,x1,y1)
            return np.ascontiguousarray(arr[y0:y1,x0:x1])
        def update_preview(*_):
            tr=max(0,choices.currentRow());target=self.regions[rows[tr]];role=role_boxes[tr].currentData();crop=make_crop(target,role)
            if crop is None:preview.setPixmap(QPixmap());preview_caption.setText("\u0644\u0627 \u062a\u062a\u0648\u0641\u0631 \u0645\u0639\u0627\u064a\u0646\u0629 \u0644\u0647\u0630\u0627 \u0627\u0644\u0645\u0635\u062f\u0631 \u0623\u0648 \u0627\u0644\u0647\u062f\u0641.");return
            rgb=np.ascontiguousarray(crop[:,:,:3] if crop.ndim==3 and crop.shape[2]>3 else crop)
            if rgb.ndim==2:rgb=cv2.cvtColor(rgb.astype(np.uint8),cv2.COLOR_GRAY2RGB)
            if rgb.ndim==3 and rgb.shape[2]==3:
                qimg=QImage(rgb.data,rgb.shape[1],rgb.shape[0],rgb.strides[0],QImage.Format_RGB888).copy()
                preview_caption.setText(f"\u0627\u0644\u0647\u062f\u0641 {target.get('id',rows[tr]+1)} | \u0627\u0644\u0645\u0635\u062f\u0631: {role_boxes[tr].currentText()}")
                preview.setPixmap(QPixmap.fromImage(qimg).scaled(preview.size(),Qt.KeepAspectRatio,Qt.SmoothTransformation))
        choices.currentCellChanged.connect(update_preview)
        for box in role_boxes:box.currentIndexChanged.connect(update_preview)
        choices.selectRow(0);update_preview()
        controls=QHBoxLayout();add=QPushButton("\u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0623\u0647\u062f\u0627\u0641 \u0627\u0644\u0645\u062d\u062f\u062f\u0629");cancel=QPushButton("\u0625\u0644\u063a\u0627\u0621")
        controls.addWidget(add);controls.addWidget(cancel);layout.addLayout(controls);cancel.clicked.connect(dialog.reject)
        def commit():
            try:
                out_dir=get_storage_path("illustrative_images");validate_storage_path(out_dir,create=True)
                order=max((int(item.get("report_order",0)) for target in self.regions for item in (target.get("report_images") or [])),default=0);saved=0
                for tr,row in enumerate(rows):
                    role=role_boxes[tr].currentData();label=role_boxes[tr].currentText()
                    if (role=="before" and self.a is None) or (role=="after" and self.b is None):
                        QMessageBox.warning(dialog,"\u0645\u0635\u062f\u0631 \u063a\u064a\u0631 \u0645\u062a\u0627\u062d",f"\u0627\u0641\u062a\u062d \u0635\u0648\u0631\u0629 \u0627\u0644\u0645\u0635\u062f\u0631 \u0627\u0644\u0645\u0637\u0644\u0648\u0628\u0629 \u0644\u0644\u0647\u062f\u0641 {self.regions[row].get('id',row+1)} \u0623\u0648\u0644\u0627\u064b.");return
                    target=self.regions[row];crop=make_crop(target,role)
                    if crop is None:continue
                    safe="".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(target.get("id",row+1)))
                    path=Path(out_dir)/f"Report_Target_{safe}_{role}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
                    rgb=np.ascontiguousarray(crop[:,:,:3] if crop.ndim==3 and crop.shape[2]>3 else crop)
                    if rgb.ndim==2:rgb=cv2.cvtColor(rgb.astype(np.uint8),cv2.COLOR_GRAY2RGB)
                    if rgb.ndim!=3 or rgb.shape[2]!=3:continue
                    engine.cv_imwrite_unicode(str(path),cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR));order+=1
                    target.setdefault("report_images",[]).append({"path":str(path),"source":role,"source_label":label,"report_order":order})
                    target["report_included"]=True;saved+=1
                self._fill_regions()
                if saved:
                    self._save_report("\u0625\u0636\u0627\u0641\u0629 \u0635\u0648\u0631 \u0623\u0647\u062f\u0627\u0641 \u0644\u062a\u0642\u0631\u064a\u0631 Word","\u0646\u062c\u0627\u062d",f"{saved} \u0647\u062f\u0641/\u0623\u0647\u062f\u0627\u0641")
                    QMessageBox.information(dialog,"\u062a\u0645\u062a \u0627\u0644\u0625\u0636\u0627\u0641\u0629",f"\u0623\u064f\u0636\u064a\u0641\u062a \u0635\u0648\u0631 {saved} \u0647\u062f\u0641/\u0623\u0647\u062f\u0627\u0641. \u0631\u0627\u062c\u0639 \u0645\u0639\u0627\u064a\u0646\u0629 \u062a\u0642\u0631\u064a\u0631 Word \u062b\u0645 \u0627\u062d\u0641\u0638 \u0627\u0644\u0645\u0644\u0641.")
                    dialog.accept()
                else:QMessageBox.warning(dialog,"\u062a\u0639\u0630\u0631\u062a \u0627\u0644\u0625\u0636\u0627\u0641\u0629","\u0644\u0645 \u064a\u062a\u0645 \u0625\u0646\u0634\u0627\u0621 \u0635\u0648\u0631 \u0644\u0644\u0623\u0647\u062f\u0627\u0641.")
            except Exception as exc:QMessageBox.critical(dialog,"\u062a\u0639\u0630\u0631\u062a \u0627\u0644\u0625\u0636\u0627\u0641\u0629",str(exc))
        add.clicked.connect(commit);dialog.exec()

    def exclude_selected_targets_from_report(self):
        selected_rows={index.row() for index in self.table.selectionModel().selectedRows()}
        rows=[row for row in self._target_selection_order if row in selected_rows]
        rows.extend(sorted(selected_rows-set(rows)))
        if not rows and self.table.currentRow() >= 0: rows=[self.table.currentRow()]
        if not rows:
            QMessageBox.information(self,'استبعاد من التقرير','حدد صفاً واحداً أو أكثر أولاً.')
            return
        for row in rows:
            if 0 <= row < len(self.regions): self.regions[row]['report_included']=False
        self._fill_regions()
        self._save_report('استبعاد أهداف من تقرير Word','نجاح',f'عدد الأهداف المستبعدة: {len(rows)}')

    def edit_region_spreadsheet(self):
        before=json.dumps((self.regions,self.report_columns,self.report_metadata),sort_keys=True,default=str)
        dialog=SpreadsheetEditorDialog(self.regions,self.report_columns,self.report_metadata,self,
                                       image_paths=(getattr(self.a,'path',''),getattr(self.b,'path','')))
        if dialog.exec()==QDialog.Accepted:
            self.regions=dialog.regions
            self.report_columns=dialog.columns
            self.report_metadata=dialog.metadata
            self._fill_regions()
            if before!=json.dumps((self.regions,self.report_columns,self.report_metadata),sort_keys=True,default=str):
                self._queue_region_excel()
            self._save_report('تعديل جدول Excel','نجاح',f'عدد الأعمدة الظاهرة: {sum(bool(c.get("visible",True)) for c in self.report_columns)}')

    def _save_analysis_tables_excel(self, analysis_dir):
        """V39: save the two Analysis tables together in a clearly named Analysis folder."""
        try:
            self._set_shared_results_mode('analysis')
            if self.region_autosave.store and not self._excel_can_leave():return None,None
            stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            tables_dir=str(get_storage_path('excel')/f'Analysis_Tables_{stamp}')
            validate_storage_path(tables_dir,create=True)
            regions_rows=self._region_excel_rows(include_all=True)
            differences_rows=self._table_as_excel_rows(self.object_table)

            regions_path=os.path.join(tables_dir,'Analysis_Change_Regions.xlsx')
            differences_path=os.path.join(tables_dir,'Analysis_What_Changed.xlsx')

            write_xlsx(regions_path,[('المناطق',regions_rows)])
            write_xlsx(differences_path,[('ما الذي اختلف؟',differences_rows)])
            if self.regions:
                columns=[c for c in self.report_columns if c.get('visible',True)]
                store=RegionWorkbook.create(regions_path,self.regions,columns,self._excel_images(),self.report_metadata)
                self.region_autosave.persisted=self.region_autosave.revision
                self.region_autosave.bind(store)
                self.statusBar().showMessage('Excel: تم ربط جدول المناطق — '+regions_path)

            self.log('تم حفظ جدول المناطق في Excel مستقل: '+regions_path)
            self.log('تم حفظ جدول ما الذي اختلف في Excel مستقل: '+differences_path)
            return regions_path,differences_path
        except Exception as exc:
            self.log('تحذير: تعذر حفظ جداول Excel المستقلة: '+str(exc))
            return None,None

    def _auto_save_verified_outputs(self):
        """Save a complete, self-contained result package for every extraction run."""
        try:
            if not getattr(self,'reporter',None) or self.a is None:
                return
            stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            analysis_root=get_storage_path('analysis_results')
            validate_storage_path(analysis_root,create=True)
            analysis_dir=os.path.join(str(analysis_root), f'Analysis_{stamp}')
            os.makedirs(analysis_dir, exist_ok=True)

            # V32 RAM SAFE: render/save one full-resolution product at a time.
            # Do not keep BEFORE + AFTER + ChangeMap copies simultaneously in RAM.
            before=self._annotate_presence_image(self.a.array,'before')
            if before is not None:
                engine.cv_imwrite_unicode(os.path.join(analysis_dir,'BEFORE_Verified.png'),cv2.cvtColor(before,cv2.COLOR_RGB2BGR))
            del before
            self._release_ram()

            if self.b is not None:
                after=self._annotate_presence_image(self.current_b(),'after')
                if after is not None:
                    engine.cv_imwrite_unicode(os.path.join(analysis_dir,'AFTER_Verified.png'),cv2.cvtColor(after,cv2.COLOR_RGB2BGR))
                del after
                self._release_ram()

            if self.mask is not None:
                cm=engine.overlay_changes(self.a.array,self.mask,0.52)
                cm=self._annotate_change_objects(cm)
                engine.cv_imwrite_unicode(os.path.join(analysis_dir,'ChangeMap_Verified.png'),cv2.cvtColor(cm,cv2.COLOR_RGB2BGR))
                del cm
                self._release_ram()
                engine.cv_imwrite_unicode(os.path.join(analysis_dir,'ChangeMask.png'),self.mask)
            if self.diff is not None:
                try:
                    hm=engine.heatmap(self.diff)
                    engine.cv_imwrite_unicode(os.path.join(analysis_dir,'Difference_Heatmap.png'),cv2.cvtColor(hm,cv2.COLOR_RGB2BGR))
                    del hm
                    self._release_ram()
                except Exception:
                    engine.cv_imwrite_unicode(os.path.join(analysis_dir,'Difference.png'),self.diff)

            payload={
                'time':stamp,
                'image1':self._image_report_info(self.a),
                'image2':self._image_report_info(self.b),
                'comparison':self.compare_meta,
                'alignment':self.last_alignment_report,
                'summary':self.last_change_stats,
                'verified_objects':self.change_objects,
                'settings':self._report_snapshot().get('settings',{}),
            }
            with open(os.path.join(analysis_dir,'Verification_Results.json'),'w',encoding='utf-8') as f:
                json.dump(payload,f,ensure_ascii=False,indent=2)

            summary_lines=[
                'ChangeVision 2026 - ملخص عملية المقارنة',
                '='*72,
                f'الوقت: {stamp}',
                f"الصورة BEFORE: {getattr(self.a,'path','')}",
                f"الصورة AFTER: {getattr(self.b,'path','') if self.b is not None else ''}",
                '', 'المواءمة:',
            ]
            summary_lines += [f'  {k}: {v}' for k,v in (self.last_alignment_report or {}).items()]
            summary_lines += ['', 'المقارنة:'] + [f'  {k}: {v}' for k,v in (self.compare_meta or {}).items()]
            summary_lines += ['', 'نتائج التغيير:'] + [f'  {k}: {v}' for k,v in (self.last_change_stats or {}).items()]
            summary_lines += ['', f'عدد مجموعات التحقق: {len(self.change_objects)}']
            with open(os.path.join(analysis_dir,'Analysis_Summary.txt'),'w',encoding='utf-8') as f:
                f.write('\n'.join(summary_lines)+'\n')

            # V35: two independent Excel files, matching the two visible result tables.
            self._save_analysis_tables_excel(analysis_dir)

            # Save a screenshot of the whole dashboard for rapid visual review.
            try:
                self.grab().save(os.path.join(analysis_dir,'Dashboard_Screenshot.png'),'PNG')
            except Exception:
                pass

            self.log('تم حفظ حزمة التحليل كاملة تلقائيًا: ' + analysis_dir)
            self.refresh_reports_panel()
        except Exception as exc:
            self.log('تحذير: تعذر حفظ حزمة التحليل التلقائية: '+str(exc))

    def _analysis_failed(self, text):
        self._wf_sequence=False
        self.task_progress.setVisible(False)
        self.statusBar().showMessage('فشلت عملية التحليل.', 6000)
        self._save_report('عملية تحليل', 'فشل', text)
        if not text.startswith('تم إيقاف'):self.err(text)
        self._wf_stage='فشل أو إيقاف التحليل';self._refresh_readiness()

    def _fill_regions(self):
        ensure_target_metadata(self.regions)
        self._region_table_loading=True
        self.table.blockSignals(True)
        self._region_edit_columns=[c for c in self.report_columns if c.get('visible',True) and excel_editable(c['key'])]
        self.table.setColumnCount(11+len(self._region_edit_columns))
        self.table.setHorizontalHeaderLabels(['انتقال','ID','المجموعة','المساحة(px)','المساحة','Longitude','Latitude','متوسط الشدة','أعلى شدة','الثقة %','التصنيف']+[c.get('title',c['key']) for c in self._region_edit_columns])
        self.table.clearContents()
        self.table.setRowCount(len(self.regions))
        for r,row in enumerate(self.regions):
            payload=dict(row)
            nav=QTableWidgetItem('🎯 انتقال')
            nav.setFlags(nav.flags()&~Qt.ItemIsEditable)
            nav.setToolTip('اضغط على الصف للانتقال إلى مركز هذا التغيير')
            nav.setData(Qt.UserRole, payload)
            self.table.setItem(r,0,nav)
            vals=[row['id'],row.get('object_id','—') if row.get('object_id') is not None else '—',f"{row['area_pixels']:,}",f"{row['area_map_units2']:,.2f}" if row['area_map_units2'] is not None else '—',f"{float(row.get('longitude')):.8f}" if row.get('longitude') is not None else '—',f"{float(row.get('latitude')):.8f}" if row.get('latitude') is not None else '—',f"{row['mean_intensity']:.3f}",f"{row['max_intensity']:.3f}",f"{row.get('confidence',0):.1f}" if 'confidence' in row else '—',row.get('classification','—')]
            for c,v in enumerate(vals, start=1):
                cell=QTableWidgetItem(str(v))
                cell.setFlags(cell.flags()&~Qt.ItemIsEditable)
                cell.setData(Qt.UserRole, payload)
                cell.setToolTip('اضغط للانتقال إلى مركز هذا التغيير')
                self.table.setItem(r,c,cell)
            for c,column in enumerate(self._region_edit_columns,11):
                value=value_for_column(row,column)
                if value in ('',None):value=self.report_metadata.get(column['key'],value)
                cell=QTableWidgetItem('' if value is None else str(value))
                cell.setData(Qt.UserRole,payload)
                self.table.setItem(r,c,cell)
        self.table.blockSignals(False);self._region_table_loading=False

    def _excel_images(self):
        return [getattr(self.a,'path',''),getattr(self.b,'path','')]

    def _queue_region_excel(self):
        self.region_autosave.queue(self.regions,self.report_columns,self.report_metadata)

    def _region_cell_committed(self,item):
        if self._region_table_loading or item.column()<11:return
        payload=item.data(Qt.UserRole) or {};uid=payload.get('target_uid')
        target=next((r for r in self.regions if r.get('target_uid')==uid),None)
        if target is None:return
        column=self._region_edit_columns[item.column()-11]
        set_column_value(target,column,item.text());self._queue_region_excel()

    def _excel_can_leave(self):
        if not self.region_autosave.dirty:return True
        self.region_autosave.flush()
        self.statusBar().showMessage('انتظر حفظ تعديلات Excel قبل تغيير الجلسة أو النتائج.')
        return False

    def _excel_saved(self):
        if self._close_after_excel:self._close_after_excel=False;self.close()

    def _excel_failed(self,error):
        if self._close_after_excel:
            self._close_after_excel=False
            QMessageBox.warning(self,'تعذر حفظ Excel','بقي البرنامج مفتوحًا لحماية آخر تعديل. أغلق الملف المقفل أو عالج المسار ثم أعد الحفظ.\n'+error)

    def _restore_region_excel(self):
        try:
            store=RegionWorkbook.discover(get_storage_path('excel'),self.regions,self._excel_images())
            if store:
                store.restore(self.regions);self.region_autosave.bind(store)
                self.report_columns=store.state.get('active_columns') or self.report_columns
                self.statusBar().showMessage('Excel: استُعيدت تعديلات الجلسة من '+str(store.path))
        except Exception as exc:self.statusBar().showMessage('Excel: تعذر الربط الآمن — '+str(exc))

    def _track_target_selection_order(self, selected, deselected):
        removed={index.row() for index in deselected.indexes()}
        if removed:
            self._target_selection_order=[row for row in self._target_selection_order if row not in removed]
        for index in selected.indexes():
            if index.row() not in self._target_selection_order:
                self._target_selection_order.append(index.row())

    def _fill_objects(self):
        self.object_table.setRowCount(len(self.change_objects))
        for r,obj in enumerate(self.change_objects):
            vals=[obj.get('object_id','—'),obj.get('change_type','مراجعة / غير مؤكد'),f"{obj.get('type_confidence',obj.get('confidence',0)):.1f}",f"{obj.get('before_presence_pct',0):.1f}",f"{obj.get('after_presence_pct',0):.1f}",f"{obj.get('cross_similarity_pct',0):.1f}",f"{obj.get('object_correspondence_pct',0):.1f}",f"{obj.get('object_edge_overlap_pct',0):.1f}",f"{obj.get('shadow_likelihood_pct',0):.1f}",f"{obj.get('area_pixels',0):,}",obj.get('parts',1),f"{obj.get('cx',0):.0f},{obj.get('cy',0):.0f}",obj.get('verification_note','')]
            for c,v in enumerate(vals): self.object_table.setItem(r,c,QTableWidgetItem(str(v)))

    def _annotate_change_objects(self, arr):
        """Draw exact polygons (default) or optional rectangles + ID/type/confidence."""
        if arr is None or not self.change_objects:
            return arr
        a=np.ascontiguousarray(arr).copy()
        if a.ndim==2:
            a=cv2.cvtColor(a,cv2.COLOR_GRAY2RGB)
        h,w=a.shape[:2]
        max_dim=max(h,w)
        line_w=max(2,int(round(max_dim/2600.0)))
        colors_rgb={
            'موجود في الصورتين (لا تغير حقيقي)':(55,200,85),
            'اختفاء حقيقي':(255,65,65),
            'ظهور حقيقي':(255,165,35),
            'تغير بنيوي/موضعي':(45,155,245),
            'ظل/حافة تابعة لجسم مطابق (مستبعد)':(182,108,255),
            'مراجعة / غير مؤكد':(175,185,195),
        }
        boundary_mode=getattr(self,'boundary_combo',None).currentText() if getattr(self,'boundary_combo',None) is not None else 'مضلعات'

        # Draw geometry with OpenCV first. Polygon mode follows the actual accepted
        # mask contour for every group; rectangle mode is kept only as an option.
        if boundary_mode != 'بدون حدود':
            for obj in self.change_objects:
                typ=obj.get('change_type','مراجعة / غير مؤكد')
                col=colors_rgb.get(typ,(255,215,70))
                if boundary_mode == 'مضلعات' and obj.get('polygons'):
                    for poly in obj.get('polygons',[]):
                        pts=np.asarray(poly,dtype=np.int32).reshape(-1,1,2)
                        if len(pts)>=3:
                            cv2.polylines(a,[pts],True,col,line_w,lineType=cv2.LINE_AA)
                else:
                    x=max(0,int(obj.get('x',0))); y=max(0,int(obj.get('y',0)))
                    bw=max(1,int(obj.get('width',1))); bh=max(1,int(obj.get('height',1)))
                    if x<w and y<h:
                        cv2.rectangle(a,(x,y),(min(w-1,x+bw-1),min(h-1,y+bh-1)),col,line_w,lineType=cv2.LINE_AA)

        q=QImage(a.data,w,h,a.strides[0],QImage.Format_RGB888).copy()
        painter=QPainter(q)
        painter.setRenderHint(QPainter.Antialiasing,True)
        font=QFont('Segoe UI'); font.setPixelSize(max(18,min(46,int(round(max_dim/430.0))))); font.setBold(True)
        painter.setFont(font)
        colors={k:QColor(*v) for k,v in colors_rgb.items()}
        for obj in self.change_objects:
            typ=obj.get('change_type','مراجعة / غير مؤكد')
            conf=float(obj.get('type_confidence',obj.get('confidence',0)))
            oid=obj.get('object_id','?')
            col=colors.get(typ,QColor(255,215,70))
            label=f'#{oid}  {typ}  {conf:.0f}%' 
            fm=painter.fontMetrics(); tw=fm.horizontalAdvance(label)+10; th=fm.height()+6
            # Put the label close to the actual polygon centroid; fall back to bbox.
            tx0=int(round(float(obj.get('cx',obj.get('x',0)))))
            ty0=int(round(float(obj.get('cy',obj.get('y',0)))))
            tx=max(0,min(tx0-tw//2,w-tw)); ty=max(0,min(ty0-th-6,h-th))

            # V34 CLEAN EXPORT LABELS:
            # draw text directly with NO rectangle, NO filled background and
            # NO black glyph outline. This applies to both on-screen Change Map
            # and ChangeMap_Verified.png export because both use this renderer.
            baseline=ty+fm.ascent()+3
            painter.setPen(QPen(col))
            painter.setBrush(Qt.NoBrush)
            painter.drawText(int(tx+5),int(baseline),label)
        painter.end()
        raw=np.frombuffer(q.bits(),dtype=np.uint8).reshape(h,q.bytesPerLine())
        return raw[:,:w*3].reshape(h,w,3).copy()

    def _annotate_presence_image(self, arr, which='before'):
        if arr is None or not self.change_objects: return arr
        a=np.ascontiguousarray(arr).copy(); h,w=a.shape[:2]
        colors={'موجود في الصورتين (لا تغير حقيقي)':(55,200,85),'اختفاء حقيقي':(255,65,65),'ظهور حقيقي':(255,165,35),'تغير بنيوي/موضعي':(45,155,245),'ظل/حافة تابعة لجسم مطابق (مستبعد)':(182,108,255),'مراجعة / غير مؤكد':(175,185,195)}
        for o in self.change_objects:
            typ=o.get('change_type','مراجعة / غير مؤكد'); col=colors.get(typ,(180,180,180))
            for poly in o.get('polygons',[]) or []:
                pts=np.asarray(poly,np.int32).reshape(-1,1,2)
                if len(pts)>=3: cv2.polylines(a,[pts],True,col,max(2,int(max(h,w)/2800)),cv2.LINE_AA)
            x=max(0,int(o.get('x',0))); y=max(18,int(o.get('y',0)))
            cv2.putText(a,f"#{o.get('object_id','?')}",(x,y-4),cv2.FONT_HERSHEY_SIMPLEX,max(.45,max(h,w)/3600.0),col,max(1,int(max(h,w)/3500)),cv2.LINE_AA)
        return a


    def _set_shared_results_mode(self, mode):
        """V39: the same two bottom tables are reused by Analysis and ArcGIS Pro."""
        mode=str(mode or 'analysis').lower()
        self.current_results_mode = 'arcgis' if mode.startswith('arc') else 'analysis'

        if hasattr(self,'results_source_label'):
            if self.current_results_mode == 'analysis':
                self.results_source_label.setText('النتائج الحالية: التحليل')
                self.results_source_label.setStyleSheet(
                    'background:#162333;border:1px solid #35506c;border-radius:5px;'
                    'padding:5px 10px;color:#8bd5ff;font-weight:700;font-size:13px;'
                )
            else:
                self.results_source_label.setText('النتائج الحالية: ArcGIS Pro')
                self.results_source_label.setStyleSheet(
                    'background:#1d2a20;border:1px solid #3d6947;border-radius:5px;'
                    'padding:5px 10px;color:#65e58a;font-weight:700;font-size:13px;'
                )

        if self.current_results_mode == 'analysis':
            self.object_table.setColumnCount(13)
            self.object_table.setHorizontalHeaderLabels([
                'المجموعة','النتيجة النهائية','الثقة %','وجود BEFORE %','وجود AFTER %',
                'تشابه الوجود %','مطابقة الجسم %','تداخل الحواف %','احتمال ظل %',
                'المساحة(px)','الأجزاء','المركز X,Y','ملاحظة التحقق'
            ])
        else:
            self.object_table.setColumnCount(9)
            self.object_table.setHorizontalHeaderLabels([
                'ID','النتيجة','المساحة m²','Longitude','Latitude',
                'نوع الهندسة','Threshold','نوع الفرق','ملاحظة'
            ])
        self.object_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)

    def _clear_shared_result_tables(self, mode='analysis'):
        self.table.clearContents()
        self.table.setRowCount(0)
        self.object_table.clearContents()
        self.object_table.setRowCount(0)
        self._set_shared_results_mode(mode)





    
    def _focus_change_row(self, row, _column=0):
        """Resolve the clicked ArcGIS row, then move Change Map after the click event finishes."""
        try:
            if row < 0 or self.a is None:
                return

            # Read the exact row attached by _fill_arcgis_polygon_rows. If for any reason
            # UserRole is unavailable, fall back to the visible Longitude/Latitude cells.
            data = None
            for col in (0, 1):
                cell = self.table.item(row, col)
                if cell is None:
                    continue
                candidate = cell.data(Qt.UserRole)
                if isinstance(candidate, dict):
                    data = dict(candidate)
                    break

            if data is None:
                data = {}
                lon_cell = self.table.item(row, 5)
                lat_cell = self.table.item(row, 6)
                try:
                    data['longitude'] = float(lon_cell.text()) if lon_cell and lon_cell.text() not in ('', '—') else None
                    data['latitude'] = float(lat_cell.text()) if lat_cell and lat_cell.text() not in ('', '—') else None
                except Exception:
                    data['longitude'] = data['latitude'] = None
                id_cell = self.table.item(row, 1)
                data['id'] = id_cell.text() if id_cell is not None else row + 1

            if self.workflow_focus_region(data,row):
                return

            lon = data.get('longitude')
            lat = data.get('latitude')
            if lon is None or lat is None:
                self.statusBar().showMessage('هذا الصف لا يحتوي Longitude/Latitude. تأكد أن الصورة GeoTIFF وبها CRS صحيح.', 5000)
                return

            # Defer the actual scene transform until Qt has completed the table mouse press.
            payload = dict(data)
            payload['longitude'] = float(lon)
            payload['latitude'] = float(lat)
            QTimer.singleShot(0, lambda p=payload: self._focus_change_row_now(p, row))
        except Exception as exc:
            self.statusBar().showMessage('تعذر قراءة صف التغيير: ' + str(exc), 6000)

    def _focus_change_row_now(self, item, row):
        """Center all synchronized viewers on one ArcGIS polygon at a clearly visible zoom."""
        try:
            lon = float(item['longitude']); lat = float(item['latitude'])
            x_geo, y_geo = lon, lat
            width = int(getattr(self.a, 'width', 0) or self.a.array.shape[1])
            height = int(getattr(self.a, 'height', 0) or self.a.array.shape[0])

            import rasterio
            from rasterio.warp import transform as rio_transform
            with rasterio.open(self.a.path) as src:
                if src.crs is not None and str(src.crs).upper() not in ('EPSG:4326', 'OGC:CRS84'):
                    xs, ys = rio_transform('EPSG:4326', src.crs, [lon], [lat])
                    x_geo, y_geo = float(xs[0]), float(ys[0])
                py, px = src.index(x_geo, y_geo)
                width, height = int(src.width), int(src.height)

            px = max(0, min(width - 1, int(px)))
            py = max(0, min(height - 1, int(py)))

            if self.mode_combo.currentText() != 'Change Map':
                self.mode_combo.setCurrentText('Change Map')
                self.refresh_result_view()

            # Precise navigation: zoom to approximately native-pixel detail, then center
            # the selected geographic point exactly in the viewport.
            def move_view(view):
                if view.item.pixmap().isNull():
                    return
                view._sync_applying = True
                try:
                    view.fitInView(view.item, Qt.KeepAspectRatio)
                    view.fit_scale = max(abs(float(view.transform().m11())), 1e-12)

                    # V43: navigation must bring the SELECTED region close enough to
                    # inspect, in BOTH Analysis and ArcGIS Pro.  About 3.5 screen
                    # pixels per source pixel gives a tight local view while keeping
                    # enough surrounding context to recognize the exact site.
                    target_scale = 3.5
                    ratio = max(1.0, min(240.0, target_scale / view.fit_scale))
                    view.scale(ratio, ratio)
                    view.zoom_factor = ratio
                    view.centerOn(float(px) + 0.5, float(py) + 0.5)
                finally:
                    view._sync_applying = False

            move_view(self.vr)
            if self._sync_enabled():
                move_view(self.va)
                move_view(self.vb)

            # V43: immediately request the Full-Resolution visible crop after
            # navigation so the selected site is sharp, not just enlarged Preview.
            self._refresh_native_detail_views()

            # Remove the previous navigation marker and draw an exact crosshair at the
            # selected polygon coordinate on Change Map. This is display-only.
            for _it in getattr(self, '_nav_marker_items', []):
                try:
                    self.vr.scene_obj.removeItem(_it)
                except Exception:
                    pass
            self._nav_marker_items = []
            marker_pen = QPen(QColor('#00ffff'))
            marker_pen.setWidthF(0.8)
            cx, cy = float(px) + 0.5, float(py) + 0.5
            # Keep the selected point unmistakable at the tighter navigation zoom.
            arm = 7.0
            circ_r = 2.2
            self._nav_marker_items.append(self.vr.scene_obj.addLine(cx-arm, cy, cx+arm, cy, marker_pen))
            self._nav_marker_items.append(self.vr.scene_obj.addLine(cx, cy-arm, cx, cy+arm, marker_pen))
            self._nav_marker_items.append(self.vr.scene_obj.addEllipse(cx-circ_r, cy-circ_r, circ_r*2, circ_r*2, marker_pen))

            self.status_coord.setText(f'X: {px}   Y: {py}   |   Lon: {lon:.8f}   Lat: {lat:.8f}')
            msg = f"انتقال إلى ID {item.get('id', row + 1)} | X={px} Y={py} | Lon={lon:.8f} Lat={lat:.8f}"
            self.statusBar().showMessage(msg, 8000)
            self.log(msg)
        except Exception as exc:
            self.statusBar().showMessage('تعذر الانتقال إلى منطقة التغيير: ' + str(exc), 7000)
            self.log('خطأ انتقال الصف: ' + str(exc))

    def _boundary_mode_changed(self, mode):
        """V41: one boundary selector controls both Analysis and ArcGIS Pro displays."""
        self.refresh_result_view()
        self._refresh_native_detail_views()
        source='ArcGIS Pro' if getattr(self,'_arcgis_change_active',False) else 'التحليل'
        self.statusBar().showMessage(f'حدود التغييرات ({source}): {mode}',3500)
        # Display updates immediately in both workflows. ArcGIS vector output itself
        # is created with the selected geometry when Polygon/Area is run.
        if hasattr(self,'arc_metric_geometry'):
            self.arc_metric_geometry.set_value(mode)
        if getattr(self,'_arcgis_change_active',False) and getattr(self,'arcgis_mask_path',None):
            self.log(f'ArcGIS Pro: تم اختيار حدود {mode}. سيتم اعتمادها عند إنشاء/تحديث الهندسة.')

    def _draw_local_boundaries(self, crop, x0, y0, role='change'):
        """Draw thin local boundaries only; never giant zoom-scaled labels."""
        if crop is None:
            return crop
        mode=self.boundary_combo.currentText() if hasattr(self,'boundary_combo') else 'مضلعات'
        if mode == 'بدون حدود':
            return crop

        out=np.ascontiguousarray(crop).copy()
        h,w=out.shape[:2]

        # ArcGIS: boundary comes directly from the true local mask.
        if getattr(self,'_arcgis_change_active',False) and self.mask is not None:
            m=np.asarray(self.mask[y0:y0+h, x0:x0+w])
            if m.ndim==3:
                m=cv2.cvtColor(m.astype(np.uint8),cv2.COLOR_RGB2GRAY)
            binary=(m>0).astype(np.uint8)
            contours,_=cv2.findContours(binary,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            col=(255,215,0)
            if mode == 'مربعات':
                for cnt in contours:
                    if cnt is None or len(cnt)<1: continue
                    xx,yy,ww,hh=cv2.boundingRect(cnt)
                    cv2.rectangle(out,(xx,yy),(max(xx,xx+ww-1),max(yy,yy+hh-1)),col,1,cv2.LINE_AA)
            elif contours:
                cv2.drawContours(out,contours,-1,col,1,cv2.LINE_AA)
            return out

        # Analysis: use verified object geometry, translated into the visible crop.
        colors={
            'موجود في الصورتين (لا تغير حقيقي)':(55,200,85),
            'اختفاء حقيقي':(255,65,65),
            'ظهور حقيقي':(255,165,35),
            'تغير بنيوي/موضعي':(45,155,245),
            'ظل/حافة تابعة لجسم مطابق (مستبعد)':(182,108,255),
            'مراجعة / غير مؤكد':(175,185,195),
        }
        for obj in (self.change_objects or []):
            ox=int(obj.get('x',0)); oy=int(obj.get('y',0))
            ow=max(1,int(obj.get('width',1))); oh=max(1,int(obj.get('height',1)))
            if ox+ow < x0 or oy+oh < y0 or ox >= x0+w or oy >= y0+h:
                continue
            col=colors.get(obj.get('change_type','مراجعة / غير مؤكد'),(255,215,70))
            if mode == 'مضلعات' and obj.get('polygons'):
                for poly in obj.get('polygons',[]) or []:
                    pts=np.asarray(poly,dtype=np.int32).reshape(-1,2)
                    if len(pts)<3: continue
                    pts=pts.copy()
                    pts[:,0]-=int(x0); pts[:,1]-=int(y0)
                    cv2.polylines(out,[pts.reshape(-1,1,2)],True,col,1,cv2.LINE_AA)
            else:
                p1=(ox-x0,oy-y0); p2=(ox+ow-1-x0,oy+oh-1-y0)
                cv2.rectangle(out,p1,p2,col,1,cv2.LINE_AA)
        return out

    def _native_detail_crop(self, role, x0, y0, x1, y1):
        """V42: return only the visible native-resolution crop for sharp zoom inspection."""
        try:
            if self.a is None:
                return None

            if role == 'before':
                src=self.a.array
            elif role == 'after':
                if self.b is None:return None
                src=self.current_b()
            else:
                src=self.a.array

            if src is None:
                return None
            h,w=src.shape[:2]
            x0=max(0,min(int(x0),w)); x1=max(x0,min(int(x1),w))
            y0=max(0,min(int(y0),h)); y1=max(y0,min(int(y1),h))
            if x1<=x0 or y1<=y0:
                return None

            crop=np.ascontiguousarray(src[y0:y1,x0:x1])
            if crop.ndim==2:
                crop=cv2.cvtColor(crop.astype(np.uint8),cv2.COLOR_GRAY2RGB)
            elif crop.ndim==3 and crop.shape[2]>3:
                crop=np.ascontiguousarray(crop[:,:,:3])

            if role == 'change':
                if self.mask is not None:
                    m=np.asarray(self.mask[y0:y1,x0:x1])
                    if getattr(self,'_arcgis_change_active',False):
                        out=np.ascontiguousarray(crop).copy()
                        mm=m
                        if mm.ndim==3:
                            mm=cv2.cvtColor(mm.astype(np.uint8),cv2.COLOR_RGB2GRAY)
                        changed=(mm>0)
                        out[changed]=np.array([255,0,0],dtype=np.uint8)
                        crop=out
                    else:
                        crop=engine.overlay_changes(crop,m,self.opacity.value()/100.0)
                elif self.diff is not None:
                    d=np.asarray(self.diff[y0:y1,x0:x1])
                    crop=engine.heatmap(d) if self.palette_combo.currentText()=='Inferno' else d

            # On all three viewers use thin geometry only. Labels stay in the normal
            # overview layer, so zoomed inspection remains clean and readable.
            if self.mask is not None:
                crop=self._draw_local_boundaries(crop,x0,y0,role)
            return np.ascontiguousarray(crop)
        except Exception:
            return None

    def _refresh_native_detail_views(self):
        for _v in (getattr(self,'va',None),getattr(self,'vb',None),getattr(self,'vr',None)):
            if _v is not None:
                _v._schedule_native_detail()

    def refresh_result_view(self):
        if self.a is None:return
        mode=self.mode_combo.currentText()
        if mode=='Overlay':
            if self.b is None:return
            arr=engine.blend_images(self.a.array,self.current_b(),self.opacity.value()/100.0)
        elif mode=='Difference':
            if self.diff is None:return
            arr=engine.heatmap(self.diff) if self.palette_combo.currentText()=='Inferno' else self.diff
        elif mode=='Change Map':
            if self.mask is None:
                if self.diff is None:return
                arr=engine.heatmap(self.diff)
            else:
                if getattr(self,'_arcgis_change_active',False):
                    arr=self._arcgis_overlay_changes(self.a.array,self.mask,self.opacity.value()/100.0)
                else:
                    arr=engine.overlay_changes(self.a.array,self.mask,self.opacity.value()/100.0)
                if getattr(self,'annotate_action',None) is not None and self.annotate_action.isChecked():
                    arr=self._annotate_change_objects(arr)
        else:
            if self.mask is not None: arr=engine.overlay_changes(self.a.array,self.mask,0.45)
            elif self.diff is not None: arr=engine.heatmap(self.diff)
            else: arr=self.a.array
        self.vr.set_array(arr)
        self._refresh_native_detail_views()

    def set_view_mode(self,mode):
        mapping={'side':'Side by Side','overlay':'Overlay','difference':'Difference','change':'Change Map'}; self.mode_combo.setCurrentText(mapping[mode]); self.refresh_result_view()
    def _mode_combo_changed(self,t): self.refresh_result_view()
    def _threshold_changed(self,v):
        # The editable spin box is synchronized with the slider.
        if hasattr(self, 'threshold_spin') and self.threshold_spin.value() != v:
            self.threshold_spin.blockSignals(True); self.threshold_spin.setValue(v); self.threshold_spin.blockSignals(False)
    def _sync_enabled(self):
        return bool(getattr(self,'sync_action',None) is not None and self.sync_action.isChecked())

    def _toggle_view_sync(self, enabled):
        if hasattr(self,'sync_badge'):
            if enabled:
                self.sync_badge.setText('🔗 Sync View: تشغيل | التحكم من أي شاشة')
                self.sync_badge.setStyleSheet('color:#6ee7b7;font-weight:700;padding:5px;')
            else:
                self.sync_badge.setText('🔓 المزامنة: إيقاف | كل نافذة مستقلة')
                self.sync_badge.setStyleSheet('color:#fbbf24;font-weight:700;padding:5px;')
        if enabled:
            self._sync_from_change_map(self.vr._view_state())
        self.statusBar().showMessage('مزامنة النوافذ: ' + ('تشغيل' if enabled else 'إيقاف'), 3500)
        if getattr(self,'reporter',None):
            self._save_report('مزامنة النوافذ', 'نجاح', 'تشغيل' if enabled else 'إيقاف')

    def _sync_from_change_map(self, state):
        if not self._sync_enabled() or not state:
            return
        # Only the result/CHANGE MAP view is the master. BEFORE and AFTER mirror it.
        self._workflow_sync_views(self.vr,state)

    def zoom_all(self,f):
        if self._sync_enabled() and not self.vr.item.pixmap().isNull():
            self.vr.zoom_by(f, emit=True)
        else:
            for v in (self.va,self.vb,self.vr): v.zoom_by(f, emit=False)

    def fit_all(self):
        if self._sync_enabled() and not self.vr.item.pixmap().isNull():
            self.vr.fit_image(emit=True)
        else:
            for v in (self.va,self.vb,self.vr): v.fit_image(emit=False)

    def actual_all(self):
        if self._sync_enabled() and not self.vr.item.pixmap().isNull():
            self.vr.actual_size(emit=True)
        else:
            for v in (self.va,self.vb,self.vr): v.actual_size(emit=False)

    def _coords(self,x,y):
        self.status_coord.setText(f'X: {x}   Y: {y}')
        sender=self.sender(); val='—'
        if getattr(sender,'array',None) is not None:
            s=max(float(getattr(sender,'preview_scale',1.0)),1e-12)
            px_i=max(0,min(sender.array.shape[1]-1,int(x*s)))
            py_i=max(0,min(sender.array.shape[0]-1,int(y*s)))
            px=sender.array[py_i,px_i]; val=str(px.tolist() if hasattr(px,'tolist') else px)
            sender.footer.setText(f'X: {x}   Y: {y}     Pixel: {val}')
            data=self._workflow_view_geometry(sender)
            if data is not None and data.transform is not None and data.crs is not None:
                gx,gy=data.transform*(x+.5,y+.5)
                sender.footer.setText(f'Pixel ({x}, {y}) | X={gx:.8f} Y={gy:.8f} | {data.crs} | Value={val}')
                self.status_coord.setText(f'Pixel ({x},{y}) | X={gx:.8f} Y={gy:.8f}')

    def new_full_session(self):
        """V39: clear results AND both source images, without closing the program."""
        if not self._excel_can_leave():return
        self.region_autosave.bind(None)
        if self._workflow_busy():
            self.log('انتظر انتهاء العملية أو إيقافها قبل بدء جلسة جديدة.');return
        try:
            self.reset_analysis()
            self.a=None
            self.b=None
            self._wf_info=[None,None];self._wf_originals=[None,None];self._wf_mode=None
            self.va.set_array(None)
            self.vb.set_array(None)
            self.vr.set_array(None)
            self.status_img.setText('الصورة: —')
            self.status_crs.setText('CRS: —')
            self.status_coord.setText('X: —   Y: —')
            self._set_shared_results_mode('analysis')
            self._release_ram()
            self.log('تم بدء جلسة جديدة كاملة. يمكنك الآن فتح BEFORE وAFTER جديدتين.')
            self._refresh_readiness()
        except Exception as e:
            self.err(e)

    def reset_analysis(self):
        """V39: reset all derived results while keeping BEFORE and AFTER open."""
        if not self._excel_can_leave():return
        self.region_autosave.bind(None)
        if self._workflow_busy():
            self.log('انتظر انتهاء العملية أو إيقافها قبل إعادة الضبط.');return
        self._remember_step();self._wf_radiometric=None;self._wf_rad_accepted=False
        self.aligned=self.H=self.valid_mask=self.analysis_valid_mask=self.diff=self.mask=None
        self.compare_meta={}
        self.regions=[]
        self.change_objects=[]
        self.last_alignment_report={}
        self.last_change_stats={}
        self.last_arcgis_alignment_report={}
        self.last_arcgis_change_stats={}
        self.arcgis_regions=[]
        self._arcgis_change_active=False
        self.arcgis_aligned_path=None
        self.arcgis_diff_path=None
        self.arcgis_mask_path=None
        self.arcgis_vector_path=None

        for _name in ('arc_metric_changed_pct','arc_metric_changed_px','arc_metric_regions',
                      'arc_metric_area','arc_metric_mean','arc_metric_max'):
            _w=getattr(self,_name,None)
            if _w is not None:_w.set_value('—')
        if hasattr(self,'arc_metric_threshold'):self.arc_metric_threshold.set_value(f'{self.arcgis_threshold.value():g}')
        if hasattr(self,'arc_metric_mode'):self.arc_metric_mode.set_value(self.arcgis_diff_mode.currentText())
        if hasattr(self,'arc_metric_geometry'):self.arc_metric_geometry.set_value(self.boundary_combo.currentText())
        if hasattr(self,'arc_metric_state'):self.arc_metric_state.set_value('جاهز')

        self._clear_shared_result_tables('analysis')
        self.vr.set_array(None)
        if self.a is not None:
            self.va.set_array(self.a.array)
        if self.b is not None:
            self.vb.set_array(self.b.array)

        for m in (
            self.metric_similarity,self.metric_change,self.metric_objects,self.metric_stable,
            self.metric_appear,self.metric_disappear,self.metric_structural,self.metric_review,
            self.metric_regions,self.metric_area,self.metric_mean,self.metric_max
        ):
            m.set_value('—')

        if hasattr(self,'stats_detail'):
            self.stats_detail.clear()
        if hasattr(self,'hist'):
            try:self.hist.set_histogram(None)
            except Exception:pass

        self._release_ram()
        self.log('تمت إعادة ضبط جميع النتائج مع إبقاء صور BEFORE وAFTER مفتوحتين.')
        self._save_report('إعادة ضبط النتائج', 'نجاح', 'تم مسح نتائج التحليل وArcGIS Pro مع إبقاء الصورتين.')
        self._refresh_readiness()

    def save_before_after_side_by_side(self):
        """Export BEFORE and AFTER side-by-side at native image detail.

        The old implementation grabbed the widget viewport, which produced a small
        screen-resolution screenshot surrounded by unused dark space.  This version
        renders the *visible scene area* directly from the QGraphicsScene at one
        output pixel per source-image pixel.  Therefore a fitted 8192x5227 image is
        exported at (approximately) its native detail, while a zoomed inspection is
        exported as a native-resolution crop of exactly that inspected area.
        """
        try:
            if self.a is None or self.b is None:
                raise RuntimeError('افتح صورتي BEFORE وAFTER أولًا.')
            if self.va.item.pixmap().isNull() or self.vb.item.pixmap().isNull():
                raise RuntimeError('لا توجد صورة ظاهرة في إحدى نافذتي BEFORE/AFTER.')

            def visible_native_render(view):
                # Convert the current viewport to scene/image coordinates, then clip
                # away any letter-box/background area surrounding the image.
                poly = view.mapToScene(view.viewport().rect())
                src = poly.boundingRect().intersected(view.item.sceneBoundingRect())
                if src.isEmpty() or src.width() < 1 or src.height() < 1:
                    src = view.item.sceneBoundingRect()

                # Native-detail export: one output pixel for one source scene unit.
                out_w = max(1, int(round(src.width())))
                out_h = max(1, int(round(src.height())))
                img = QImage(out_w, out_h, QImage.Format_RGB888)
                img.fill(QColor('#10151d'))
                pp = QPainter(img)
                pp.setRenderHint(QPainter.Antialiasing, True)
                pp.setRenderHint(QPainter.SmoothPixmapTransform, True)
                view.scene().render(
                    pp,
                    QRectF(0, 0, out_w, out_h),
                    src,
                    Qt.IgnoreAspectRatio,
                )
                pp.end()
                return img

            before_img = visible_native_render(self.va)
            after_img = visible_native_render(self.vb)
            if before_img.isNull() or after_img.isNull():
                raise RuntimeError('تعذر إنشاء صورة عالية الدقة من BEFORE/AFTER.')

            # No huge decorative canvas: only a compact header + small gap.
            gap = 10
            outer = 8
            header_h = 56
            w1, h1 = before_img.width(), before_img.height()
            w2, h2 = after_img.width(), after_img.height()
            content_h = max(h1, h2)
            total_w = outer * 2 + w1 + gap + w2
            total_h = outer * 2 + header_h + content_h

            # Guard against accidental pathological dimensions while preserving native
            # detail for normal satellite images (including 8192x5227 pairs).
            if total_w > 40000 or total_h > 20000 or total_w * total_h > 180_000_000:
                raise RuntimeError(
                    f'حجم التصدير كبير جدًا ({total_w}×{total_h}). قرّب إلى المنطقة المطلوبة ثم أعد الحفظ.'
                )

            canvas = QImage(total_w, total_h, QImage.Format_RGB888)
            canvas.fill(QColor('#0f151d'))
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.Antialiasing, True)
            font = QFont()
            font.setPixelSize(26)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor('#e9f1fa'))
            painter.drawText(QRectF(outer, outer, w1, header_h), Qt.AlignCenter, 'BEFORE  |  صورة قبل')
            painter.drawText(QRectF(outer + w1 + gap, outer, w2, header_h), Qt.AlignCenter, 'AFTER  |  صورة بعد')

            y = outer + header_h
            # Keep each image at native rendered size; center vertically only when the
            # visible crop heights differ slightly between the two views.
            painter.drawImage(outer, y + (content_h - h1)//2, before_img)
            painter.drawImage(outer + w1 + gap, y + (content_h - h2)//2, after_img)
            painter.end()

            out_dir = get_storage_path('illustrative_images')
            validate_storage_path(out_dir,create=True)
            stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            default_path = str(out_dir / f'Before_After_SideBySide_{stamp}.png')
            path, selected_filter = QFileDialog.getSaveFileName(
                self,
                'حفظ صورة BEFORE / AFTER جنبًا إلى جنب - دقة عالية',
                default_path,
                'PNG (*.png);;JPEG (*.jpg *.jpeg);;TIFF (*.tif *.tiff)'
            )
            if not path:
                return

            selected_filter = selected_filter or ''
            root, ext = os.path.splitext(path)
            ext_l = ext.lower()
            if not ext_l:
                if 'JPEG' in selected_filter:
                    path = root + '.jpg'; ext_l = '.jpg'
                elif 'TIFF' in selected_filter:
                    path = root + '.tif'; ext_l = '.tif'
                else:
                    path = root + '.png'; ext_l = '.png'

            if ext_l not in ('.jpg','.jpeg','.png','.tif','.tiff'):
                path=root+'.png';ext_l='.png'
            self._protect_originals(path)
            if not confirm_overwrite(self,path):return
            validate_storage_path(Path(path).parent,create=True)
            def save_canvas(temp_path, image_format, quality=None):
                ok = canvas.save(str(temp_path), image_format, quality) if quality is not None else canvas.save(str(temp_path), image_format)
                if not ok:
                    raise OSError(f'فشل ترميز الصورة بصيغة {image_format}.')
            ok = False
            if ext_l in ('.jpg', '.jpeg'):
                try:atomic_write(path,lambda temp: save_canvas(temp,'JPEG',98),overwrite=True)
                except Exception as exc:raise RuntimeError(f'تعذر حفظ JPEG: {exc}') from exc
                ok=True
            elif ext_l in ('.tif', '.tiff'):
                try:
                    atomic_write(path,lambda temp: save_canvas(temp,'TIFF'),overwrite=True)
                    ok=True
                except Exception:
                    ok=False
                if not ok:
                    tmp = os.path.join(tempfile.gettempdir(), f'cv_side_{stamp}.png')
                    try:
                        if not canvas.save(tmp, 'PNG'):
                            raise RuntimeError('تعذر إنشاء ملف وسيط لتصدير TIFF.')
                        img = cv2.imread(tmp, cv2.IMREAD_COLOR)
                        if img is None:
                            raise RuntimeError('تعذر قراءة الملف الوسيط لتصدير TIFF.')
                        def save_cv_tiff(temp):
                            if not cv2.imwrite(str(temp),img):raise OSError('فشل ترميز TIFF بواسطة OpenCV.')
                        atomic_write(path,save_cv_tiff,overwrite=True)
                        ok=True
                    finally:
                        try:
                            if os.path.isfile(tmp): os.remove(tmp)
                        except Exception:
                            pass
            else:
                if ext_l != '.png':
                    path = root + '.png'
                atomic_write(path,lambda temp: save_canvas(temp,'PNG'),overwrite=True)
                ok=True

            if not ok:
                raise RuntimeError('تعذر حفظ صورة BEFORE/AFTER بالصيغة المختارة.')

            detail = f'{path} | export={total_w}×{total_h} | before={w1}×{h1} | after={w2}×{h2}'
            self.log('تم حفظ BEFORE/AFTER جنبًا إلى جنب بالدقة الأصلية/المرئية: ' + detail)
            self._save_report('حفظ BEFORE/AFTER جنبًا إلى جنب - دقة عالية', 'نجاح', detail)
            QMessageBox.information(
                self,
                'تم الحفظ',
                'تم حفظ BEFORE وAFTER جنبًا إلى جنب بدقة عالية.\n\n'
                f'الأبعاد: {total_w} × {total_h} px\n\n{path}'
            )
        except Exception as e:
            self.err(e)

    def _safe_export_mask_geotiff(self, out_path):
        """V38: write the analysis mask as a GIS-safe GeoTIFF without inherited YCbCr tags."""
        if self.a is None or self.a.profile is None or self.mask is None:
            return None
        self._protect_originals(out_path)
        import rasterio
        profile=dict(self.a.profile)
        # Never inherit RGB/JPEG/YCBCR display compression into a one-band mask.
        for k in ('photometric','PHOTOMETRIC','jpeg_quality','JPEG_QUALITY',
                  'source_color_space','SOURCE_COLOR_SPACE','interleave','INTERLEAVE'):
            profile.pop(k,None)
        profile.update(
            driver='GTiff',
            height=int(self.mask.shape[0]),
            width=int(self.mask.shape[1]),
            count=1,
            dtype='uint8',
            compress='DEFLATE',
            nodata=0
        )
        with rasterio.open(out_path,'w',**profile) as dst:
            dst.write(np.asarray(self.mask,dtype=np.uint8),1)
        return out_path

    def _copy_if_exists(self, src, dst_dir, dst_name=None):
        if src and os.path.isfile(src):
            dst=os.path.join(dst_dir,dst_name or os.path.basename(src))
            shutil.copy2(src,dst)
            return dst
        return None

    def _export_analysis_results(self, folder):
        """Export only Analysis results into the automatically named Analysis folder."""
        if self.a is None:
            raise RuntimeError('لا توجد صورة مرجعية.')
        if self.mask is None:
            raise RuntimeError('نفّذ كشف التغيير في تبويب التحليل أولًا.')

        overlay=engine.overlay_changes(self.a.array,self.mask,0.48)
        engine.cv_imwrite_unicode(
            os.path.join(folder,'Analysis_Change_Map.png'),
            cv2.cvtColor(overlay,cv2.COLOR_RGB2BGR)
        )
        engine.cv_imwrite_unicode(os.path.join(folder,'Analysis_Change_Mask.png'),self.mask)

        if self.change_objects:
            numbered=self._annotate_change_objects(overlay)
            engine.cv_imwrite_unicode(
                os.path.join(folder,'Analysis_Change_Objects.png'),
                cv2.cvtColor(numbered,cv2.COLOR_RGB2BGR)
            )

        if self.diff is not None:
            engine.cv_imwrite_unicode(os.path.join(folder,'Analysis_Difference.png'),self.diff)

        if self.a.profile is not None:
            self._safe_export_mask_geotiff(os.path.join(folder,'Analysis_Change_Mask.tif'))
            engine.save_geojson(
                os.path.join(folder,'Analysis_Changes.geojson'),
                self.mask,self.a,self.diff
            )

        # Two Excel files exactly matching the two Analysis result tables.
        write_xlsx(
            os.path.join(folder,'Analysis_Change_Regions.xlsx'),
            [('المناطق',self._region_excel_rows(include_all=True))]
        )
        write_xlsx(
            os.path.join(folder,'Analysis_What_Changed.xlsx'),
            [('ما الذي اختلف؟',self._table_as_excel_rows(self.object_table))]
        )

        with open(os.path.join(folder,'Analysis_Summary.json'),'w',encoding='utf-8') as f:
            json.dump({
                'comparison':self.compare_meta,
                'regions':self.regions,
                'change_objects':self.change_objects,
                'settings':{
                    'method':self.method.currentText(),
                    'threshold':self.threshold.value(),
                    'min_area':self.min_area.value(),
                    'kernel':self.kernel.value(),
                    'edge_tolerance':self.edge_tolerance.value(),
                    'accept_confidence':self.accept_conf.value(),
                    'review_confidence':self.review_conf.value(),
                    'merge_distance':self.merge_distance.value()
                }
            },f,ensure_ascii=False,indent=2)


    def export_all(self):
        """Export Analysis-only results."""
        try:
            parent=QFileDialog.getExistingDirectory(self,'اختر المجلد الرئيسي للتصدير',str(get_storage_path('exports')))
            if not parent:
                return
            validate_storage_path(parent,create=True)
            stamp=datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
            folder=os.path.join(parent,'Export_Analysis_'+stamp)
            os.makedirs(folder,exist_ok=False)
            self._export_analysis_results(folder)
            self.log(f'تم تصدير نتائج التحليل إلى: {folder}')
            self._save_report('تصدير النتائج', 'نجاح', folder)
            QMessageBox.information(self,'تم التصدير',f'تم تصدير نتائج التحليل بنجاح.\n\n{folder}')
        except Exception as e:
            self.err(e)

    def save_project(self):
        try:
            if self.a is None or self.b is None: raise RuntimeError('افتح الصورتين قبل حفظ المشروع.')
            suggested=self.current_project or str(get_storage_path('projects')/'ChangeVision_Project.cvp')
            p,_=QFileDialog.getSaveFileName(self,'حفظ المشروع',suggested,'ChangeVision Project (*.cvp)')
            if not p:return
            if not p.lower().endswith('.cvp'):p+='.cvp'
            if not confirm_overwrite(self,p):return
            asset_choices=['حفظ المراجع الخارجية فقط (ملف أصغر)','تضمين نسخ الصور والملفات المرتبطة داخل CVP']
            asset_choice,accepted=QInputDialog.getItem(self,'أصول مشروع CVP','اختر طريقة حفظ الصور والملفات المرتبطة:',asset_choices,0,False)
            if not accepted:return
            include_assets=asset_choice==asset_choices[1]
            project_dir=Path(p).resolve().parent
            def asset_ref(value):
                if not isinstance(value,str) or not value:return None
                try:
                    source=Path(value).resolve()
                    # Relative references also cover sibling folders on the
                    # same drive; this makes a moved project tree relocatable.
                    return Path(os.path.relpath(source,project_dir)).as_posix()
                except (OSError,ValueError):pass
                return None
            project_regions=json.loads(json.dumps(self.regions,ensure_ascii=False,default=str))
            refs={'image1':asset_ref(self.a.path),'image2':asset_ref(self.b.path),'targets':[]}
            for index,target in enumerate(project_regions):
                target_refs={}
                def collect_refs(node,prefix=''):
                    if isinstance(node,dict):
                        for key,value in node.items():
                            child=f'{prefix}/{key}' if prefix else key
                            if isinstance(value,str) and (key.endswith('path') or key.endswith('_image')):
                                relative=asset_ref(value)
                                if relative:target_refs[child]=relative
                            else:collect_refs(value,child)
                    elif isinstance(node,list):
                        for pos,value in enumerate(node):collect_refs(value,f'{prefix}/{pos}')
                collect_refs(target)
                if target_refs:refs['targets'].append({'index':index,'paths':target_refs})
            d={'schema_version':4,'image1':self.a.path,'image2':self.b.path,'asset_refs':refs,'method':self.method.currentText(),'threshold':self.threshold.value(),'min_area':self.min_area.value(),'kernel':self.kernel.value(),'edge_tolerance':self.edge_tolerance.value(),'accept_confidence':self.accept_conf.value(),'review_confidence':self.review_conf.value(),'merge_distance':self.merge_distance.value(),'opacity':self.opacity.value(),'aligned':self.aligned is not None,
               'regions':project_regions,'change_objects':self.change_objects,'change_stats':self.last_change_stats,
               'compare_meta':self.compare_meta,'alignment_report':self.last_alignment_report,
               'report_columns':self.report_columns,'report_metadata':self.report_metadata}
            if self.mask is not None:
                ok,encoded=cv2.imencode('.png',np.asarray(self.mask,dtype=np.uint8))
                if ok:d['analysis_mask_png']=encoded.tobytes().hex()
            if self.diff is not None:
                ok,encoded=cv2.imencode('.png',np.asarray(self.diff))
                if ok:d['analysis_diff_png']=encoded.tobytes().hex()
            d['preparation']=self.workflow_project_state()
            raw_size, packed_size = write_project(p,d,include_assets=include_assets)
            mode='مع تضمين الأصول' if include_assets else 'بالمراجع الخارجية'
            self.current_project=p; self.log(f'تم حفظ المشروع {mode}: {p} ({packed_size:,} / {raw_size:,} بايت)'); self._save_report('حفظ المشروع', 'نجاح', p)
        except Exception as e:self.err(e)

    def open_project(self):
        if not self._excel_can_leave():return
        p,_=QFileDialog.getOpenFileName(self,'فتح مشروع','','ChangeVision Project (*.cvp)')
        if not p:return
        try:
            d=read_project(p)
            project_dir=Path(p).resolve().parent
            refs=d.get('asset_refs') or {}
            for key,label in (('image1','الصورة قبل'),('image2','الصورة بعد')):
                saved=d.get(key,'')
                relative=refs.get(key)
                candidate=(project_dir/relative).resolve() if relative else None
                if candidate and candidate.is_file():
                    d[key]=str(candidate)
                elif not Path(saved).is_file():
                    QMessageBox.information(self,'إعادة ربط صورة مفقودة',
                        f'تعذر العثور على {label} المرتبطة بالمشروع. اختر الملف لإعادة الربط.')
                    selected,_=QFileDialog.getOpenFileName(self,f'إعادة ربط {label}',
                        str(Path(saved).parent) if saved else str(project_dir),'All files (*)')
                    if not selected:
                        raise FileNotFoundError(f'لم تتم إعادة ربط {label}; لم يُفتح المشروع.')
                    d[key]=selected
            missing_target_assets=[]
            for target_ref in refs.get('targets',[]):
                index=target_ref.get('index',-1)
                if not 0<=index<len(d.get('regions') or []):continue
                target=d['regions'][index]
                for pointer,relative in target_ref.get('paths',{}).items():
                    parts=pointer.split('/');node=target
                    try:
                        for part in parts[:-1]:node=node[int(part)] if isinstance(node,list) else node[part]
                        candidate=(project_dir/relative).resolve()
                        if candidate.is_file():node[parts[-1]]=str(candidate)
                        elif isinstance(node.get(parts[-1]),str) and not Path(node[parts[-1]]).is_file():
                            selected,_=QFileDialog.getOpenFileName(
                                self,'إعادة ربط صورة مرتبطة بهدف',str(Path(node[parts[-1]]).parent),
                                'Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp);;All files (*)')
                            if selected:node[parts[-1]]=selected
                            else:missing_target_assets.append(str(node[parts[-1]]))
                    except (KeyError,IndexError,TypeError,ValueError):continue
            if missing_target_assets:
                QMessageBox.warning(self,'مراجع صور مفقودة',
                    f'تعذر إعادة ربط {len(missing_target_assets)} من صور الأهداف. '
                    'ستبقى بيانات الأهداف محفوظة، ويمكن اختيار الصور لاحقاً من إدارة الأهداف.')
            self._load_a(d['image1']); self._load_b(d['image2']); self.method.setCurrentText(d.get('method','ROBUST FINAL')); self.threshold.setValue(d.get('threshold',35)); self.min_area.setValue(d.get('min_area',100)); self.kernel.setValue(d.get('kernel',3)); self.edge_tolerance.setValue(d.get('edge_tolerance',3)); self.accept_conf.setValue(d.get('accept_confidence',60)); self.review_conf.setValue(d.get('review_confidence',45)); self.merge_distance.setValue(d.get('merge_distance',12)); self.opacity.setValue(d.get('opacity',50))
            self.report_columns=d.get('report_columns') or [dict(column) for column in DEFAULT_COLUMNS]
            self.report_metadata=d.get('report_metadata') or {}
            self.compare_meta=dict(d.get('compare_meta') or {})
            self.last_alignment_report=dict(d.get('alignment_report') or {})
            self._pending_project_analysis={key:d.get(key) for key in
                ('regions','change_objects','change_stats','analysis_mask_png','analysis_diff_png')}
            wf=d.get('preparation') or {}
            self._wf_originals=wf.get('originals',self._wf_originals)
            self._wf_declarations=wf.get('declarations',{})
            self._wf_mode=wf.get('analysis_mode')
            self.workflow_inspect()
            has_saved_analysis=bool(d.get('regions') or d.get('change_objects') or
                                    d.get('analysis_mask_png') or d.get('analysis_diff_png'))
            if d.get('aligned') and not has_saved_analysis:
                self.align()
            else:
                # Restore saved review outputs without automatically repeating
                # a costly registration when a project already contains results.
                self._restore_project_analysis(self._pending_project_analysis)
                self._pending_project_analysis=None
            self.current_project=p; self.log('تم فتح المشروع: '+p); self._save_report('فتح المشروع', 'نجاح', p)
        except Exception as e:self.err(e)

    def _restore_project_analysis(self, state):
        """Restore saved review data when present; legacy CVP files remain valid."""
        if not state:
            return
        self.regions=ensure_target_metadata(list(state.get('regions') or []))
        self.change_objects=list(state.get('change_objects') or [])
        self.last_change_stats=dict(state.get('change_stats') or {})
        for field,attribute in (('analysis_mask_png','mask'),('analysis_diff_png','diff')):
            encoded=state.get(field)
            if encoded:
                try:
                    raw=np.frombuffer(bytes.fromhex(encoded),dtype=np.uint8)
                    setattr(self,attribute,cv2.imdecode(raw,cv2.IMREAD_UNCHANGED))
                except (ValueError,TypeError):
                    setattr(self,attribute,None)
        self._restore_region_excel()
        self._fill_regions()
        self._fill_objects()
        if self.mask is not None:
            self.refresh_result_view()
















    def _show_gis_tools_tab(self):
        try:
            # right_tabs: التحليل، GIS Tools، التقارير
            if hasattr(self,'right_tabs'):
                self.right_tabs.setCurrentIndex(next((i for i in range(self.right_tabs.count()) if self.right_tabs.tabText(i)=='GIS Tools'),0))
        except Exception:
            pass

    def gis_inspect_pair(self):
        try:
            if self.a is None or self.b is None:
                raise RuntimeError('افتح الصورتين أولًا.')
            info=gis_tools.inspect_pair(self.a.path,self.b.path)
            text=gis_tools.format_pair_report(info)
            self.gis_status.setText('✓ تم الفحص بنجاح')
            self.stats_detail.setPlainText(text)
            self.log('GIS: تم فحص CRS / Pixel Size / Extent / Overlap.')
            self._save_report('GIS فحص الصورتين','نجاح',text)
            QMessageBox.information(self,'GIS فحص الصورتين',text[:3500])
        except Exception as e:
            if hasattr(self,'gis_status'): self.gis_status.setText('✗ فشل الفحص: '+str(e))
            self.err(e)

    def gis_crop_overlap(self):
        self.workflow_advanced_crop()

    def gis_align_to_reference(self):
        self.workflow_advanced_grid()

    def gis_inspect_radiometry(self):
        try:
            self.need()
            self._workflow_assert_idle();self._workflow_radiometric_gate()
            self._wf_stage='فحص الإضاءة والألوان'
            if self._worker_running(self._radiometric_inspect_worker):
                return
            if self._worker_running(self._align_worker):
                raise RuntimeError('انتظر انتهاء المواءمة الهندسية ثم أعد فحص الإضاءة والألوان.')
            alignment = None
            if self.aligned is not None:
                self._require_accurate_alignment()
                alignment = {'report': dict(self.last_alignment_report), 'H': self.H,
                             'aligned': self.aligned, 'valid_mask': self.valid_mask}
            self._radiometric_inspect_context = (self.a, self.b, self.aligned)
            self.gis_inspect_colors_button.setEnabled(False)
            self.gis_status.setText('جاري فحص الشبكة والمواءمة والإضاءة والألوان...')
            worker = RadiometricInspectWorker(self.a.path, self.b.path, alignment, self)
            self._radiometric_inspect_worker = worker
            worker.completed.connect(self._radiometric_inspect_done)
            worker.failed.connect(self._radiometric_inspect_failed)
            worker.finished.connect(self._radiometric_inspect_finished)
            worker.start()
            self._refresh_readiness()
        except Exception as exc:
            self.gis_inspect_colors_button.setEnabled(True)
            self.err(exc)

    def _radiometric_context_current(self):
        context = self._radiometric_inspect_context
        return context is not None and all(old is new for old, new in
                                          zip(context, (self.a, self.b, self.aligned)))

    def _radiometric_inspect_done(self, result):
        if not self._radiometric_context_current():
            self.gis_status.setText('تغيرت الصور أو المواءمة أثناء الفحص؛ أعد الفحص للصور الحالية.')
            return
        text = radiometric_inspection.format_report(result)
        self._remember_step();self._wf_radiometric=dict(result);self._wf_rad_accepted=False
        self._wf_stage='اكتمل فحص الإضاءة والألوان';self._refresh_readiness()
        self.gis_status.setText(result['recommendation'])
        self.stats_detail.setPlainText(text)
        self.log('GIS فحص الإضاءة والألوان: ' + result['recommendation'])
        self._save_report('GIS فحص الإضاءة والألوان', 'نجاح', text)
        if self._wf_sequence:return
        dialog = QMessageBox(self)
        dialog.setWindowTitle('فحص الإضاءة والألوان')
        dialog.setIcon(QMessageBox.Information)
        dialog.setText(result['recommendation'])
        dialog.setInformativeText(
            f"Brightness difference %: {result['brightness_difference_percent']:.2f}\n"
            f"Contrast difference %: {result['contrast_difference_percent']:.2f}\n"
            f"Color difference %: {result['color_difference_percent']:.2f}\n"
            f"Histogram difference %: {result['histogram_difference_percent']:.2f}\n"
            'لم يتم تنفيذ Radiometric. يمكنك تشغيل زر التوحيد بنفسك عند الحاجة.')
        dialog.setDetailedText(text)
        dialog.exec()

    def _radiometric_inspect_failed(self, text):
        self._wf_sequence=False
        if self._radiometric_context_current():
            self.gis_status.setText('تعذر فحص الإضاءة والألوان: ' + text)
            if not text.startswith('تم إيقاف'):self.err(text)

    def _radiometric_inspect_finished(self):
        self._dispose_worker('_radiometric_inspect_worker')
        self._radiometric_inspect_context = None
        self._refresh_readiness()

    def closeEvent(self, event):
        self.table.setFocus()
        if self.region_autosave.dirty:
            self._close_after_excel=True;self.region_autosave.flush();event.ignore();return
        if self._workflow_busy():
            self.statusBar().showMessage('انتظر انتهاء العملية أو اطلب إيقافها قبل إغلاق البرنامج.')
            event.ignore()
            return
        super().closeEvent(event)

    def gis_radiometric_match(self):
        self.workflow_advanced_radiometric()

    def _update_device(self):
        """Detect NVIDIA GPU without importing PyTorch.

        Importing torch into the same GUI process can initialize Intel OpenMP
        (libiomp5md.dll), while OpenCV/SIFT may initialize LLVM OpenMP
        (libomp.dll). On Windows this can abort the whole application with
        OMP Error #15. nvidia-smi avoids loading either runtime.
        """
        try:
            candidates = [
                shutil.which('nvidia-smi'),
                os.path.join(os.environ.get('ProgramFiles', ''),
                             'NVIDIA Corporation', 'NVSMI', 'nvidia-smi.exe'),
                os.path.join(os.environ.get('WINDIR', ''),
                             'System32', 'nvidia-smi.exe'),
            ]
            exe = next((x for x in candidates if x and os.path.exists(x)), None)
            if not exe:
                self.device_lbl.setText('● CPU / GPU غير مفحوص')
                return
            cp = subprocess.run(
                [exe, '--query-gpu=name', '--format=csv,noheader'],
                capture_output=True, text=True, timeout=3,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            name = (cp.stdout or '').strip().splitlines()[0] if cp.returncode == 0 and (cp.stdout or '').strip() else ''
            self.device_lbl.setText('● GPU  ' + name if name else '● CPU / GPU غير مفحوص')
        except Exception:
            self.device_lbl.setText('● CPU / GPU غير مفحوص')

if __name__=='__main__':
    if '--runtime-diagnostics' in sys.argv:
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        import runtime_diagnostics
        runtime_diagnostics.run(sys.argv[sys.argv.index('--runtime-diagnostics')+1],smoke=True)
        sys.exit(0)
    runtime_paths.set_windows_app_id()
    app=QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setLayoutDirection(Qt.RightToLeft)

    # Ensure Qt always has a valid application point size.
    # This prevents: QFont::setPointSize: Point size <= 0 (-1)
    _app_font = app.font()
    if _app_font.pointSize() <= 0:
        _app_font.setFamily('Segoe UI')
        _app_font.setPointSize(9)
        app.setFont(_app_font)

    while True:
        try:
            w=MainWindow()
            break
        except (OSError, ValueError) as exc:
            QMessageBox.warning(None, 'تعذر الوصول إلى التخزين',
                                'تحقق من اتصال القرص وصلاحيات الكتابة، أو اختر مسارًا آخر من إدارة التخزين.\n\n' + str(exc))
            if StorageSettingsDialog().exec()!=QDialog.Accepted:
                sys.exit(0)
    w.show(); sys.exit(app.exec())
