"""Layered raster annotation editor; source files are never written in place."""
from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF, QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGraphicsPixmapItem,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget, QColorDialog, QFontComboBox, QDoubleSpinBox,
)

from image_formats import read_analysis_image
from storage_manager import atomic_write, confirm_overwrite, get_storage_path, validate_storage_path


def _to_qimage(rgb):
    arr = np.ascontiguousarray(rgb)
    return QImage(arr.data, arr.shape[1], arr.shape[0], arr.strides[0], QImage.Format_RGB888).copy()

def adjusted_rgb(rgb,brightness=0,contrast=100,sharpness=0):
    image=np.clip(rgb.astype(np.float32)*(contrast/100.0)+brightness,0,255).astype(np.uint8)
    sharp=sharpness/100.0
    if sharp:image=cv2.addWeighted(image,1+sharp,cv2.GaussianBlur(image,(0,0),1.2),-sharp,0)
    return image


def draw_report_annotation(painter, annotation):
    """Shared report renderer for canvas, PDF and Word; coordinates are image pixels."""
    points=annotation.get('points') or []
    if not points:return
    painter.save()
    xs=[p[0] for p in points];ys=[p[1] for p in points];cx=(min(xs)+max(xs))/2;cy=(min(ys)+max(ys))/2
    painter.translate(cx,cy);painter.rotate(float(annotation.get('rotation',0)));painter.translate(-cx,-cy)
    pen=QPen(QColor(annotation.get('color','#ff3030')),max(.5,float(annotation.get('size',3))))
    pen.setStyle(Qt.DashLine if annotation.get('line_style')=='dash' else Qt.SolidLine);pen.setCapStyle(Qt.RoundCap);painter.setPen(pen)
    fill=QColor(annotation.get('fill_color','#ffffff'));fill.setAlphaF(max(0,min(1,float(annotation.get('fill_opacity',0)))));painter.setBrush(fill)
    kind=annotation.get('type')
    if kind in ['line','arrow'] and len(points)>1:
        x1,y1=points[0];x2,y2=points[-1];painter.drawLine(QPointF(x1,y1),QPointF(x2,y2))
        if kind=='arrow':
            angle=np.arctan2(y2-y1,x2-x1);length=max(2,float(annotation.get('arrow_size',12)))
            for offset in [-.55,.55]:painter.drawLine(QPointF(x2-length*np.cos(angle+offset),y2-length*np.sin(angle+offset)),QPointF(x2,y2))
    elif kind in ['rectangle','circle','ellipse'] and len(points)>1:
        rect=QRectF(QPointF(*points[0]),QPointF(*points[-1])).normalized()
        if kind=='rectangle':painter.drawRect(rect)
        else:
            if kind=='circle':rect.setHeight(rect.width())
            painter.drawEllipse(rect)
    elif kind in ['freehand','polygon']:
        poly=QPolygonF([QPointF(*p) for p in points])
        if kind=='polygon':painter.drawPolygon(poly)
        else:painter.drawPolyline(poly)
    elif kind=='text':
        font=QFont(annotation.get('font_family','Arial'));font.setPixelSize(max(8,int(annotation.get('font_size',24))));painter.setFont(font)
        text=str(annotation.get('text',''));rect=QFontMetricsF(font).boundingRect(text).translated(points[0][0],points[0][1])
        bg=QColor(annotation.get('text_background','#ffffff'));bg.setAlphaF(max(0,min(1,float(annotation.get('text_background_opacity',0)))));painter.fillRect(rect.adjusted(-3,-3,3,3),bg)
        painter.drawText(QPointF(*points[0]),text)
    painter.restore()


class AnnotationCanvas(QGraphicsView):
    text_requested = Signal(float, float)
    annotation_selected = Signal(int)
    crop_selected = Signal(object)
    erase_requested = Signal(int)
    before_edit = Signal()
    annotations_changed = Signal(object)

    TOOLS = ('select', 'crop', 'arrow', 'line', 'rectangle', 'circle', 'freehand', 'text', 'eraser')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.base_item = QGraphicsPixmapItem()
        self.overlay_item = QGraphicsPixmapItem()
        self.overlay_item.setZValue(10)
        self.scene_obj.addItem(self.base_item)
        self.scene_obj.addItem(self.overlay_item)
        self.image_shape = (0, 0)
        self.annotations = []
        self.tool = 'select'
        self.color = '#ff3030'
        self.font_size = 24
        self.drawing = False
        self.start = None
        self.current = None
        self.selected_index = -1
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setBackgroundBrush(QColor('#111923'))
        self.report_mode=False;self.report_style={};self.report_drag=None;self.polygon_points=[]

    def set_base(self, rgb):
        self.image_shape = rgb.shape[:2]
        self.base_item.setPixmap(QPixmap.fromImage(_to_qimage(rgb)))
        self.scene_obj.setSceneRect(QRectF(0, 0, rgb.shape[1], rgb.shape[0]))
        self.render_annotations()
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def set_annotations(self, annotations):
        self.annotations = copy.deepcopy(annotations or [])
        self.selected_index = -1
        self.render_annotations()

    def _draw_annotation(self, painter, annotation):
        if annotation.get('report_schema')==1:
            draw_report_annotation(painter,annotation);return
        points = annotation.get('points') or []
        if not points:
            return
        painter.setPen(QPen(QColor(annotation.get('color', '#ff3030')),
                            max(1, int(annotation.get('size', 3))), Qt.SolidLine,
                            Qt.RoundCap, Qt.RoundJoin))
        kind = annotation.get('type')
        if kind in ('line', 'arrow') and len(points) >= 2:
            x1, y1 = points[0]; x2, y2 = points[-1]
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))
            if kind == 'arrow':
                angle = np.arctan2(y2-y1, x2-x1)
                length = max(8, int(annotation.get('size', 3)) * 4)
                for offset in (-0.55, 0.55):
                    x = x2 - length*np.cos(angle+offset)
                    y = y2 - length*np.sin(angle+offset)
                    painter.drawLine(int(x), int(y), int(x2), int(y2))
        elif kind in ('rectangle', 'circle', 'crop') and len(points) >= 2:
            x1, y1 = points[0]; x2, y2 = points[-1]
            rect = QRectF(min(x1,x2), min(y1,y2), abs(x2-x1), abs(y2-y1))
            if kind == 'rectangle': painter.drawRect(rect)
            else: painter.drawEllipse(rect)
        elif kind == 'freehand':
            for first, second in zip(points, points[1:]):
                painter.drawLine(int(first[0]), int(first[1]), int(second[0]), int(second[1]))
        elif kind == 'text':
            painter.setPen(QPen(QColor(annotation.get('color', '#ff3030'))))
            font = painter.font(); font.setPixelSize(max(8, int(annotation.get('font_size', self.font_size))))
            painter.setFont(font)
            painter.drawText(int(points[0][0]), int(points[0][1]), str(annotation.get('text', '')))

    def render_annotations(self, preview=None):
        height, width = self.image_shape
        if width <= 0 or height <= 0:
            return
        image = QImage(width, height, QImage.Format_ARGB32)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        for index, annotation in enumerate(self.annotations):
            self._draw_annotation(painter, annotation)
            if index == self.selected_index:
                pts = annotation.get('points') or []
                if pts:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    painter.setPen(QPen(QColor('#35a7ff'), 2, Qt.DashLine))
                    painter.drawRect(QRectF(min(xs)-5, min(ys)-5,
                                            max(12, max(xs)-min(xs)+10), max(12, max(ys)-min(ys)+10)))
        if preview and self.start and self.current:
            ghost = {'type': preview, 'points': [self.start, self.current], 'color': self.color, 'size': 2}
            self._draw_annotation(painter, ghost)
        painter.end()
        self.overlay_item.setPixmap(QPixmap.fromImage(image))

    def _point(self, event):
        point = self.mapToScene(event.position().toPoint())
        h, w = self.image_shape
        return (float(np.clip(point.x(), 0, max(0,w-1))),
                float(np.clip(point.y(), 0, max(0,h-1))))

    def _hit_test(self, point):
        px, py = point
        for index in range(len(self.annotations)-1, -1, -1):
            annotation=self.annotations[index]
            pts=annotation.get('points') or []
            if not pts:continue
            if self.report_mode:
                xs=[p[0] for p in pts];ys=[p[1] for p in pts]
                cx=(min(xs)+max(xs))/2;cy=(min(ys)+max(ys))/2;angle=-np.deg2rad(float(annotation.get('rotation',0)))
                dx,dy=point[0]-cx,point[1]-cy;qx=cx+dx*np.cos(angle)-dy*np.sin(angle);qy=cy+dx*np.sin(angle)+dy*np.cos(angle)
                box=QRectF(min(xs)-10,min(ys)-10,max(20,max(xs)-min(xs)+20),max(20,max(ys)-min(ys)+20))
                if annotation.get('type')=='text':
                    font=QFont(annotation.get('font_family','Arial'));font.setPixelSize(max(8,int(annotation.get('font_size',24))));box=QFontMetricsF(font).boundingRect(str(annotation.get('text',''))).translated(pts[0][0],pts[0][1]).adjusted(-10,-10,10,10)
                if box.contains(QPointF(qx,qy)):return index
                continue
            kind=annotation.get('type')
            if kind=='text':
                distances=[np.hypot(px-pts[0][0],py-pts[0][1])]
            elif kind in ('rectangle','crop') and len(pts)>=2:
                (x1,y1),(x2,y2)=pts[:2]
                edges=[((x1,y1),(x2,y1)),((x2,y1),(x2,y2)),((x2,y2),(x1,y2)),((x1,y2),(x1,y1))]
                distances=[self._segment_distance(point,a,b) for a,b in edges]
            elif kind=='circle' and len(pts)>=2:
                (x1,y1),(x2,y2)=pts[:2];rx=max(1,abs(x2-x1)/2);ry=max(1,abs(y2-y1)/2)
                cx=(x1+x2)/2;cy=(y1+y2)/2
                distances=[abs(np.hypot((px-cx)/rx,(py-cy)/ry)-1)*min(rx,ry)]
            elif len(pts)>=2:
                distances=[self._segment_distance(point,a,b) for a,b in zip(pts,pts[1:])]
            else:
                distances=[np.hypot(px-pts[0][0],py-pts[0][1])]
            if min(distances) < max(12, self.font_size):
                return index
        return -1

    @staticmethod
    def _segment_distance(point, first, second):
        px,py=point;x1,y1=first;x2,y2=second
        dx,dy=x2-x1,y2-y1
        length=dx*dx+dy*dy
        if length<=1e-9:return float(np.hypot(px-x1,py-y1))
        t=float(np.clip(((px-x1)*dx+(py-y1)*dy)/length,0,1))
        return float(np.hypot(px-(x1+t*dx),py-(y1+t*dy)))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        point = self._point(event)
        self.start = point; self.current = point
        if self.report_mode and not self.overlay_item.isVisible():return super().mousePressEvent(event)
        if self.report_mode and self.tool=='pan':return super().mousePressEvent(event)
        if self.report_mode and self.tool=='polygon':
            if not self.polygon_points:self.before_edit.emit()
            self.polygon_points.append(point);return
        if self.report_mode and self.tool=='number':
            self.before_edit.emit();self.annotations.append({'id':str(uuid.uuid4()),'type':'text','points':[point],'text':str(1+sum(bool(a.get('numbered')) for a in self.annotations)),'numbered':True,'report_schema':1,**self.report_style})
            self.annotations_changed.emit(copy.deepcopy(self.annotations));self.render_annotations();return
        if self.tool == 'select':
            self.selected_index = self._hit_test(point)
            self.render_annotations()
            self.annotation_selected.emit(self.selected_index)
            if self.report_mode and self.selected_index>=0:
                pts=self.annotations[self.selected_index].get('points',[]);xs=[p[0] for p in pts];ys=[p[1] for p in pts]
                resize=len(pts)>1 and np.hypot(point[0]-max(xs),point[1]-max(ys))<12
                self.report_drag={'points':copy.deepcopy(pts),'start':point,'resize':resize,'checkpoint':False}
            return
        if self.tool == 'text':
            self.text_requested.emit(*point)
            return
        if self.tool == 'eraser':
            index = self._hit_test(point)
            if index >= 0:
                self.selected_index = index
                self.annotation_selected.emit(index)
                self.erase_requested.emit(index)
            return
        self.before_edit.emit()
        self.drawing = True
        if self.tool == 'freehand':
            self.current_annotation = {'id': str(uuid.uuid4()), 'type': 'freehand', 'points': [point],
                                       'color': self.color, 'size': self.font_size}

    def mouseMoveEvent(self, event):
        if self.report_mode and self.tool=='select' and self.report_drag and event.buttons()&Qt.LeftButton:
            if not self.report_drag['checkpoint']:self.before_edit.emit();self.report_drag['checkpoint']=True
            p=self._point(event);old=self.report_drag['points'];sx,sy=self.report_drag['start'];dx,dy=p[0]-sx,p[1]-sy
            if self.report_drag['resize']:
                x0=min(x for x,y in old);y0=min(y for x,y in old);w=max(x for x,y in old)-x0;h=max(y for x,y in old)-y0
                fx=max(.05,(w+dx)/max(1,w));fy=max(.05,(h+dy)/max(1,h))
                if self.annotations[self.selected_index].get('type')=='circle':fx=fy=max(fx,fy)
                pts=[(x0+(x-x0)*fx,y0+(y-y0)*fy) for x,y in old]
            else:pts=[(x+dx,y+dy) for x,y in old]
            self.annotations[self.selected_index]['points']=pts;self.render_annotations();return
        if self.drawing:
            self.current = self._point(event)
            if self.tool == 'freehand': self.current_annotation['points'].append(self.current)
            self.render_annotations(self.tool)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.report_mode and self.report_drag:
            self.annotations_changed.emit(copy.deepcopy(self.annotations));self.report_drag=None;return
        if not self.drawing or event.button() != Qt.LeftButton:
            return super().mouseReleaseEvent(event)
        self.current = self._point(event)
        if self.tool == 'crop':
            x1,y1=self.start; x2,y2=self.current
            self.crop_selected.emit((int(min(x1,x2)),int(min(y1,y2)),
                                     int(abs(x2-x1)),int(abs(y2-y1))))
        else:
            if self.tool == 'freehand':
                self.current_annotation['points'].append(self.current)
                annotation = self.current_annotation
            else:
                annotation = {'id': str(uuid.uuid4()), 'type': self.tool,
                              'points': [self.start,self.current], 'color': self.color,
                              'size': self.font_size}
            if len(annotation.get('points', [])):
                if self.report_mode:
                    annotation.update(copy.deepcopy(self.report_style));annotation['report_schema']=1
                    if annotation['type']=='circle':
                        (x,y),(x2,y2)=annotation['points'];diam=min(abs(x2-x),abs(y2-y));annotation['points']=[(x,y),(x+np.sign(x2-x)*diam,y+np.sign(y2-y)*diam)]
                self.annotations.append(annotation)
                self.selected_index = len(self.annotations)-1
                self.annotation_selected.emit(self.selected_index)
                if self.report_mode:self.annotations_changed.emit(copy.deepcopy(self.annotations))
        self.drawing = False
        self.render_annotations()

    def mouseDoubleClickEvent(self,event):
        if self.report_mode and self.tool=='polygon':
            pts=[]
            for p in self.polygon_points:
                if not pts or pts[-1]!=p:pts.append(p)
            if len(pts)>=3:
                self.annotations.append({'id':str(uuid.uuid4()),'type':'polygon','points':pts,'report_schema':1,**copy.deepcopy(self.report_style)})
                self.annotations_changed.emit(copy.deepcopy(self.annotations));self.render_annotations()
            self.polygon_points=[];return
        super().mouseDoubleClickEvent(event)


class ImageEditorDialog(QDialog):
    """Original/working/annotation/composite layers with editable project state."""
    def __init__(self, image_path, layer_state=None, parent=None, *, report_mode=False, image_rgb=None, original_rgb=None):
        super().__init__(parent)
        self.setWindowTitle('محرر الصور والطبقات')
        self.resize(1180, 820)
        self.report_mode=report_mode
        self.image_path = os.fspath(image_path) if image_path is not None else 'report-image'
        self.layer_state = copy.deepcopy(layer_state or {})
        self.report_transforms=copy.deepcopy(self.layer_state.get('transforms',[]))
        source = read_analysis_image(self.image_path) if image_rgb is None else None
        self.original = np.ascontiguousarray(source.array.copy() if source is not None else image_rgb.copy())
        if report_mode and original_rgb is not None:self.original=np.ascontiguousarray(original_rgb.copy())
        working_path = self.layer_state.get('working_path')
        if working_path and Path(working_path).is_file():
            self.base = np.ascontiguousarray(read_analysis_image(working_path).array.copy())
        else:
            self.base = np.ascontiguousarray(image_rgb.copy()) if image_rgb is not None else self.original.copy()
        self.annotations = copy.deepcopy(self.layer_state.get('annotations', []))
        self.brightness = int(self.layer_state.get('brightness', 0))
        self.contrast = int(self.layer_state.get('contrast', 100))
        self.sharpness = int(self.layer_state.get('sharpness', 0))
        self.history = []
        self.redo_history = []
        self.color = self.layer_state.get('color', '#ff3030')
        root = QVBoxLayout(self)
        self.canvas = AnnotationCanvas(self)
        self.canvas.report_mode=report_mode
        if report_mode:self.canvas.annotations_changed.connect(self._report_annotations_changed)
        self.canvas.set_annotations(self.annotations)
        self.canvas.text_requested.connect(self.add_text)
        self.canvas.annotation_selected.connect(self.select_annotation)
        self.canvas.crop_selected.connect(self.apply_crop)
        self.canvas.erase_requested.connect(self.erase_annotation)
        self.canvas.before_edit.connect(self.checkpoint)
        area = QScrollArea(); area.setWidgetResizable(True); area.setWidget(self.canvas)
        root.addWidget(area, 1)
        controls = QHBoxLayout()
        self.tool = QComboBox()
        self.tool.addItem('تحديد/تحرير', 'select')
        for label, key in (('قص','crop'),('سهم','arrow'),('خط','line'),('مستطيل','rectangle'),
                           ('دائرة','circle'),('رسم حر','freehand'),('نص','text'),('ممحاة','eraser')):
            self.tool.addItem(label,key)
        self.tool.currentIndexChanged.connect(lambda *_: setattr(self.canvas,'tool',self.tool.currentData()))
        controls.addWidget(self.tool)
        self.layer_view=QComboBox()
        for label,key in (('الصورة الأصلية','original'),('نسخة العمل','working'),
                          ('الرسومات فقط','annotations'),('الصورة المركبة','composite')):
            self.layer_view.addItem(label,key)
        self.layer_view.setCurrentIndex(1)
        if report_mode and self.layer_state.get('original_available') is False:self.layer_view.setItemText(0,'أساس مركب؛ الأصل غير متوفر')
        self.layer_view.currentIndexChanged.connect(self.refresh_canvas)
        controls.addWidget(QLabel('الطبقة'));controls.addWidget(self.layer_view)
        self.color_button = QPushButton('اللون')
        self.color_button.clicked.connect(self.choose_color); controls.addWidget(self.color_button)
        self.size = QSpinBox(); self.size.setRange(1,120); self.size.setValue(self.canvas.font_size)
        self.size.valueChanged.connect(self.set_size); controls.addWidget(QLabel('حجم الخط/الخط')); controls.addWidget(self.size)
        self.edit_button = QPushButton('تعديل العنصر'); self.edit_button.clicked.connect(self.edit_annotation); controls.addWidget(self.edit_button)
        self.delete_button = QPushButton('حذف العنصر'); self.delete_button.clicked.connect(self.delete_annotation); controls.addWidget(self.delete_button)
        self.undo_button = QPushButton('تراجع'); self.undo_button.clicked.connect(self.undo); controls.addWidget(self.undo_button)
        self.redo_button = QPushButton('إعادة'); self.redo_button.clicked.connect(self.redo); controls.addWidget(self.redo_button)
        root.addLayout(controls)
        adjustments = QHBoxLayout()
        self.brightness_slider = self._slider(-100,100,self.brightness,'السطوع')
        self.contrast_slider = self._slider(0,200,self.contrast,'التباين')
        self.sharp_slider = self._slider(0,200,self.sharpness,'الوضوح')
        for slider in (self.brightness_slider,self.contrast_slider,self.sharp_slider):
            adjustments.addWidget(slider)
        rotate_left = QPushButton('تدوير يسار'); rotate_left.clicked.connect(lambda: self.rotate(False)); adjustments.addWidget(rotate_left)
        rotate_right = QPushButton('تدوير يمين'); rotate_right.clicked.connect(lambda: self.rotate(True)); adjustments.addWidget(rotate_right)
        self.zoom = QSlider(Qt.Horizontal); self.zoom.setRange(25,300); self.zoom.setValue(100); self.zoom.setMaximumWidth(130)
        self.zoom.valueChanged.connect(self.set_zoom); adjustments.addWidget(QLabel('تكبير')); adjustments.addWidget(self.zoom)
        root.addLayout(adjustments)
        self.layer_label = QLabel('الطبقات: أصلية محفوظة | نسخة عمل | رسومات مستقلة')
        root.addWidget(self.layer_label)
        self.status = QLabel(self.image_path); self.status.setWordWrap(True); root.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save_layers); buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self.selected_annotation = -1
        self._connect_adjustment(self.brightness_slider)
        self._connect_adjustment(self.contrast_slider)
        self._connect_adjustment(self.sharp_slider)
        if report_mode:self._setup_report_tools(root)
        self.refresh_canvas()

    def _slider(self, low, high, value, label):
        box=QWidget(); layout=QVBoxLayout(box); layout.setContentsMargins(2,2,2,2)
        layout.addWidget(QLabel(label)); slider=QSlider(Qt.Horizontal); slider.setRange(low,high); slider.setValue(value)
        layout.addWidget(slider); box.slider=slider
        return box

    def _connect_adjustment(self, wrapper):
        slider=wrapper.slider
        slider.sliderPressed.connect(self.checkpoint)
        slider.valueChanged.connect(self.refresh_canvas)

    def _adjusted(self):
        return adjusted_rgb(self.base,self.brightness_slider.slider.value(),self.contrast_slider.slider.value(),self.sharp_slider.slider.value())

    def refresh_canvas(self, *_):
        mode=self.layer_view.currentData() if hasattr(self,'layer_view') else 'working'
        if mode=='original':
            display=self.original
            annotations=[]
            self.canvas.overlay_item.setVisible(False)
        elif mode=='annotations':
            display=np.full_like(self.base,32)
            annotations=self.annotations
            self.canvas.overlay_item.setVisible(True)
        elif mode=='composite':
            display=self._composite()
            annotations=[]
            self.canvas.overlay_item.setVisible(False)
        else:
            display=self._adjusted()
            annotations=self.annotations
            self.canvas.overlay_item.setVisible(True)
        self.canvas.set_annotations(annotations)
        self.canvas.color=self.color
        self.canvas.font_size=self.size.value() if hasattr(self,'size') else 24
        self.canvas.set_base(display)
        if self.report_mode and hasattr(self,'report_font'):self._report_style_changed()

    def set_zoom(self, value):
        self.canvas.resetTransform()
        self.canvas.scale(value/100.0,value/100.0)

    def set_size(self, value):
        self.canvas.font_size=int(value)

    def choose_color(self):
        color=QColorDialog.getColor(QColor(self.color),self,'اختيار لون الرسم')
        if color.isValid():
            self.color=color.name(); self.canvas.color=self.color
            if self.report_mode:self._report_style_changed()

    def checkpoint(self):
        self.history.append({'base':self.base.copy(),'annotations':copy.deepcopy(self.annotations),
                             'brightness':self.brightness_slider.slider.value(),
                             'contrast':self.contrast_slider.slider.value(),
                             'sharpness':self.sharp_slider.slider.value(),
                             'transforms':copy.deepcopy(self.report_transforms)})
        if len(self.history)>12:self.history.pop(0)
        self.redo_history.clear()

    def _restore(self,state):
        self.base=state['base'].copy(); self.annotations=copy.deepcopy(state['annotations'])
        if self.report_mode:self.report_transforms=copy.deepcopy(state.get('transforms',[]))
        for wrapper,key in ((self.brightness_slider,'brightness'),(self.contrast_slider,'contrast'),(self.sharp_slider,'sharpness')):
            wrapper.slider.blockSignals(True); wrapper.slider.setValue(state[key]); wrapper.slider.blockSignals(False)
        self.refresh_canvas()

    def undo(self):
        if not self.history:return
        self.redo_history.append({'base':self.base.copy(),'annotations':copy.deepcopy(self.annotations),
                                 'brightness':self.brightness_slider.slider.value(),
                                 'contrast':self.contrast_slider.slider.value(),
                                 'sharpness':self.sharp_slider.slider.value(),'transforms':copy.deepcopy(self.report_transforms)})
        self._restore(self.history.pop())

    def redo(self):
        if not self.redo_history:return
        self.history.append({'base':self.base.copy(),'annotations':copy.deepcopy(self.annotations),
                             'brightness':self.brightness_slider.slider.value(),
                             'contrast':self.contrast_slider.slider.value(),
                             'sharpness':self.sharp_slider.slider.value(),'transforms':copy.deepcopy(self.report_transforms)})
        self._restore(self.redo_history.pop())

    def apply_crop(self, rect):
        x,y,w,h=rect
        if w<2 or h<2:return
        self.checkpoint()
        self.base=self.base[y:y+h,x:x+w].copy()
        if self.report_mode:
            self.report_transforms.append({'type':'crop','window':[x,y,w,h]})
            for a in self.annotations:a['points']=[(px-x,py-y) for px,py in a.get('points',[])]
            self.refresh_canvas();return
        for annotation in self.annotations:
            annotation['points']=[(px-x,py-y) for px,py in annotation.get('points',[]) if x<=px<x+w and y<=py<y+h]
        self.annotations=[a for a in self.annotations if a.get('points')]
        self.refresh_canvas()

    def rotate(self, clockwise):
        self.checkpoint(); old_h,old_w=self.base.shape[:2]
        if self.report_mode:self.report_transforms.append({'type':'rotate90','clockwise':bool(clockwise),'input_dimensions':[old_w,old_h]})
        self.base=cv2.rotate(self.base,cv2.ROTATE_90_CLOCKWISE if clockwise else cv2.ROTATE_90_COUNTERCLOCKWISE)
        for annotation in self.annotations:
            converted=[]
            for x,y in annotation.get('points',[]):
                converted.append((old_h-1-y,x) if clockwise else (y,old_w-1-x))
            annotation['points']=converted
        self.refresh_canvas()

    def add_text(self,x,y):
        text,ok=QInputDialog.getText(self,'إضافة تعليق','النص أو اسم الهدف:')
        if ok and text.strip():
            self.checkpoint()
            self.annotations.append({'id':str(uuid.uuid4()),'type':'text','points':[(x,y)],
                                     'text':text.strip(),'color':self.color,'font_size':self.size.value()})
            if self.report_mode:self.annotations[-1].update(report_schema=1,**self.canvas.report_style)
            self.refresh_canvas()

    def select_annotation(self,index):
        self.selected_annotation=index
        self.canvas.selected_index=index
        self.canvas.render_annotations()
        if self.report_mode and 0<=index<len(self.canvas.annotations):
            a=self.canvas.annotations[index];self.color=a.get('color',self.color);self.size.setValue(int(a.get('size',3)))
            self.report_font.setCurrentFont(QFont(a.get('font_family','Arial')));self.report_font_size.setValue(int(a.get('font_size',24)));self.report_rotation.setValue(float(a.get('rotation',0)))
            self.report_arrow.setValue(int(a.get('arrow_size',12)));self.report_opacity.setValue(int(100*a.get('fill_opacity',0)));self.report_bg_opacity.setValue(int(100*a.get('text_background_opacity',0)))
            self.report_line.setCurrentText(a.get('line_style','solid'));self.report_fill_color=a.get('fill_color','#ffffff');self.report_bg_color=a.get('text_background','#ffffff')
            pts=a.get('points') or [(0,0)];xs=[p[0] for p in pts];ys=[p[1] for p in pts]
            self.report_x.setValue(min(xs));self.report_y.setValue(min(ys));self.report_w.setValue(max(1,max(xs)-min(xs)));self.report_h.setValue(max(1,max(ys)-min(ys)))

    def edit_annotation(self):
        if self.report_mode:return self._apply_report_properties(edit_text=True)
        index=self.selected_annotation
        if not 0<=index<len(self.annotations):return
        annotation=self.annotations[index]
        if annotation.get('type')=='text':
            text,ok=QInputDialog.getText(self,'تعديل التعليق','النص:',text=annotation.get('text',''))
            if not ok:return
        self.checkpoint()
        if annotation.get('type')=='text':annotation['text']=text
        self.annotations[index]['color']=self.color
        self.annotations[index]['size']=self.size.value()
        if self.annotations[index].get('type')=='text':self.annotations[index]['font_size']=self.size.value()
        self.refresh_canvas()

    def delete_annotation(self):
        if 0<=self.selected_annotation<len(self.annotations):
            self.checkpoint();self.annotations.pop(self.selected_annotation);self.selected_annotation=-1;self.refresh_canvas()

    def erase_annotation(self,index):
        if 0<=index<len(self.annotations):
            self.checkpoint();self.annotations.pop(index);self.selected_annotation=-1;self.refresh_canvas()

    def _composite(self):
        image=self._adjusted().copy()
        height,width=image.shape[:2]
        layer=QImage(width,height,QImage.Format_ARGB32);layer.fill(Qt.transparent)
        painter=QPainter(layer);painter.setRenderHint(QPainter.Antialiasing,True)
        for annotation in self.annotations:self.canvas._draw_annotation(painter,annotation)
        painter.end()
        rgba=layer.convertToFormat(QImage.Format_RGBA8888)
        bits=rgba.constBits(); overlay=np.frombuffer(bits,dtype=np.uint8,count=rgba.sizeInBytes()).reshape(rgba.height(),rgba.bytesPerLine())[:,:rgba.width()*4].reshape(rgba.height(),rgba.width(),4)
        alpha=overlay[:,:,3:4].astype(np.float32)/255
        return np.clip(image*(1-alpha)+overlay[:,:,:3]*alpha,0,255).astype(np.uint8)

    def save_layers(self):
        if self.report_mode:
            transforms=copy.deepcopy(self.report_transforms)
            adjustment={'type':'display_adjustment','brightness':self.brightness_slider.slider.value(),'contrast':self.contrast_slider.slider.value(),'sharpness':self.sharp_slider.slider.value()}
            if (adjustment['brightness'],adjustment['contrast'],adjustment['sharpness'])!=(0,100,0):transforms.append(adjustment)
            self.result={'working_rgb':self._adjusted().copy(),'annotations':copy.deepcopy(self.annotations),'transforms':transforms}
            self.accept();return
        folder=get_storage_path('illustrative_images')
        validate_storage_path(folder,create=True)
        suggested=str(folder/(Path(self.image_path).stem+'_annotated.png'))
        path,_=QFileDialog.getSaveFileName(self,'حفظ الصورة المركبة',suggested,'PNG (*.png);;TIFF (*.tif *.tiff);;JPEG (*.jpg *.jpeg)')
        if not path:return
        ext=Path(path).suffix.lower()
        if ext not in ('.png','.tif','.tiff','.jpg','.jpeg'):path+='.png';ext='.png'
        base_path=str(Path(path).with_name(Path(path).stem+'_working.png'))
        if not confirm_overwrite(self,path) or not confirm_overwrite(self,base_path):return
        def write_rgb(temp,image,encoded_ext):
            params=[cv2.IMWRITE_JPEG_QUALITY,95] if encoded_ext in ('.jpg','.jpeg') else []
            ok,data=cv2.imencode(encoded_ext,cv2.cvtColor(image,cv2.COLOR_RGB2BGR),params)
            if not ok:raise OSError('تعذر ترميز طبقة الصورة.')
            data.tofile(str(temp))
        atomic_write(base_path,lambda temp: write_rgb(temp,self.base.copy(),'.png'),overwrite=True)
        composite=self._composite()
        encoded_ext='.jpg' if ext in ('.jpg','.jpeg') else ('.tif' if ext in ('.tif','.tiff') else '.png')
        atomic_write(path,lambda temp: write_rgb(temp,composite,encoded_ext),overwrite=True)
        self.result={'original_path':self.image_path,'working_path':base_path,
                     'composite_path':path,'annotations':copy.deepcopy(self.annotations),
                     'brightness':self.brightness_slider.slider.value(),
                     'contrast':self.contrast_slider.slider.value(),
                     'sharpness':self.sharp_slider.slider.value(),'color':self.color}
        self.accept()

    def _report_annotations_changed(self,annotations):
        self.annotations=copy.deepcopy(annotations)

    def _setup_report_tools(self,root):
        for label,key in [('بيضاوي','ellipse'),('ترقيم','number'),('مضلع (نقر مزدوج للإنهاء)','polygon'),('تحريك العرض','pan')]:self.tool.addItem(label,key)
        self.report_fill_color='#ffffff';self.report_bg_color='#ffffff'
        props=QFormLayout();root.insertLayout(root.count()-1,props)
        self.report_font=QFontComboBox();self.report_font.setCurrentFont(QFont('Arial'));self.report_font_size=QSpinBox();self.report_font_size.setRange(8,160);self.report_font_size.setValue(24)
        self.report_line=QComboBox();self.report_line.addItems(['solid','dash']);self.report_arrow=QSpinBox();self.report_arrow.setRange(2,160);self.report_arrow.setValue(12)
        self.report_opacity=QSpinBox();self.report_opacity.setRange(0,100);self.report_bg_opacity=QSpinBox();self.report_bg_opacity.setRange(0,100)
        self.report_rotation=QDoubleSpinBox();self.report_rotation.setRange(-360,360)
        self.report_x=QDoubleSpinBox();self.report_y=QDoubleSpinBox();self.report_w=QDoubleSpinBox();self.report_h=QDoubleSpinBox()
        for spin in [self.report_x,self.report_y]:spin.setRange(-100000,100000)
        for spin in [self.report_w,self.report_h]:spin.setRange(.1,100000);spin.setValue(30)
        def pair(*widgets):
            w=QWidget();layout=QHBoxLayout(w);layout.setContentsMargins(0,0,0,0)
            for item in widgets:layout.addWidget(item)
            return w
        props.addRow('خط النص / حجمه',pair(self.report_font,self.report_font_size));props.addRow('نمط الخط / رأس السهم',pair(self.report_line,self.report_arrow))
        props.addRow('شفافية التعبئة % / خلفية النص %',pair(self.report_opacity,self.report_bg_opacity));props.addRow('موضع X / Y (بكسل الصورة)',pair(self.report_x,self.report_y));props.addRow('عرض / ارتفاع / دوران',pair(self.report_w,self.report_h,self.report_rotation))
        commands=QHBoxLayout();root.insertLayout(root.count()-1,commands)
        for title,fn in [('لون التعبئة',lambda:self._report_pick('fill')),('خلفية النص',lambda:self._report_pick('background')),('تطبيق خصائص المحدد',self._apply_report_properties),('تكرار',self._report_duplicate),('للأمام',lambda:self._report_order(1)),('للخلف',lambda:self._report_order(-1)),('ملاءمة',lambda:self.canvas.fitInView(self.canvas.sceneRect(),Qt.KeepAspectRatio)),('حجم فعلي',lambda:self.set_zoom(100))]:
            b=QPushButton(title);b.clicked.connect(lambda _=False,action=fn:action());commands.addWidget(b)
        self.size.setValue(3)
        for signal in [self.size.valueChanged,self.report_font.currentFontChanged,self.report_font_size.valueChanged,self.report_line.currentIndexChanged,self.report_arrow.valueChanged,self.report_opacity.valueChanged,self.report_bg_opacity.valueChanged]:signal.connect(self._report_style_changed)
        self._report_style_changed()

    def _report_style_changed(self,*_):
        self.canvas.report_style={'color':self.color,'size':self.size.value(),'font_family':self.report_font.currentFont().family(),
          'font_size':self.report_font_size.value(),'line_style':self.report_line.currentText(),'arrow_size':self.report_arrow.value(),
          'fill_color':self.report_fill_color,'fill_opacity':self.report_opacity.value()/100,
          'text_background':self.report_bg_color,'text_background_opacity':self.report_bg_opacity.value()/100}

    def _report_pick(self,kind):
        c=QColorDialog.getColor(QColor(self.report_fill_color if kind=='fill' else self.report_bg_color),self)
        if c.isValid():
            if kind=='fill':self.report_fill_color=c.name()
            else:self.report_bg_color=c.name()
            self._report_style_changed()

    def _apply_report_properties(self,edit_text=False):
        index=self.selected_annotation
        if not 0<=index<len(self.annotations):return
        a=copy.deepcopy(self.annotations[index]);text=a.get('text','')
        if edit_text and a.get('type')=='text':
            text,ok=QInputDialog.getText(self,'تحرير نص العنصر','النص',text=text)
            if not ok:return
        self.checkpoint();self._report_style_changed();a.update(copy.deepcopy(self.canvas.report_style));a['report_schema']=1;a['rotation']=self.report_rotation.value()
        if a.get('type')=='text':a['text']=text
        pts=a.get('points') or [(0,0)];x0=min(x for x,y in pts);y0=min(y for x,y in pts);w=max(x for x,y in pts)-x0;h=max(y for x,y in pts)-y0
        width=self.report_w.value();height=width if a.get('type')=='circle' else self.report_h.value()
        a['points']=[(self.report_x.value()+(x-x0)*width/max(1,w),self.report_y.value()+(y-y0)*height/max(1,h)) for x,y in pts]
        self.annotations[index]=a;self.refresh_canvas();self.selected_annotation=index;self.canvas.selected_index=index;self.canvas.render_annotations()

    def _report_duplicate(self):
        if not 0<=self.selected_annotation<len(self.annotations):return
        self.checkpoint();a=copy.deepcopy(self.annotations[self.selected_annotation]);a['id']=str(uuid.uuid4());a['points']=[(x+10,y+10) for x,y in a.get('points',[])];self.annotations.append(a);self.refresh_canvas()

    def _report_order(self,delta):
        i=self.selected_annotation;j=i+delta
        if 0<=i<len(self.annotations) and 0<=j<len(self.annotations):
            self.checkpoint();self.annotations[i],self.annotations[j]=self.annotations[j],self.annotations[i];self.refresh_canvas();self.selected_annotation=j;self.canvas.selected_index=j;self.canvas.render_annotations()
