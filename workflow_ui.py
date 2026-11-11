"""Guided preparation UI, isolated from existing image processing algorithms."""
from __future__ import annotations
import copy
import json
import math
import os
import time
from pathlib import Path
import numpy as np
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QPen, QColor
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QScrollArea,
                               QTextEdit,QInputDialog,QMessageBox,QFileDialog)
import preparation
import runtime_paths
from storage_manager import (
    commit_staged_output, confirm_overwrite, get_storage_path,
    stage_output_path, validate_storage_path,
)


class PreparationWorker(QThread):
    completed=Signal(object)
    failed=Signal(str)
    progress=Signal(int,str)

    def __init__(self,job,parent=None):
        super().__init__(parent);self.job=job

    def checkpoint(self):
        preparation.check_cancel(self.isInterruptionRequested)

    def report(self,value,text):
        self.checkpoint();self.progress.emit(min(99,int(value)),str(text))

    def run(self):
        try:
            self.checkpoint()
            result=self.job(self)
            self.checkpoint();self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.job=None


class WorkflowMixin:
    def _init_workflow(self):
        self._wf_info=[None,None];self._wf_originals=[None,None]
        self._wf_declarations={};self._wf_mode=None;self._wf_radiometric=None
        self._wf_rad_accepted=False;self._wf_undo=None;self._wf_internal=False
        self._wf_task=None;self._wf_adopt=None;self._wf_stage='فتح الصورتين'
        self._wf_state={};self._wf_syncing=False;self._wf_full_view=None
        self._wf_sequence=False;self._wf_sequence_seen=set()

    def _workflow_bar(self):
        box=QWidget();layout=QHBoxLayout(box)
        text=QVBoxLayout();self.workflow_current=QLabel('الخطوة الحالية: فتح الصورتين')
        self.workflow_next=QLabel('الخطوة التالية الموصى بها: فتح الصورتين')
        for label in (self.workflow_current,self.workflow_next):label.setWordWrap(True);text.addWidget(label)
        layout.addLayout(text,1)
        self.workflow_next_button=QPushButton('تنفيذ الخطوة التالية');self.workflow_next_button.clicked.connect(self.workflow_next_action)
        layout.addWidget(self.workflow_next_button)
        self.workflow_sequence_button=QPushButton('تنفيذ المعالجات المطلوبة بالتسلسل')
        self.workflow_sequence_button.clicked.connect(self.workflow_sequence_start)
        layout.addWidget(self.workflow_sequence_button)
        return box

    def _workflow_panel(self):
        page=QScrollArea();page.setWidgetResizable(True)
        content=QWidget();layout=QVBoxLayout(content)
        self.readiness_badge=QLabel('❌ NOT READY');self.readiness_badge.setWordWrap(True);layout.addWidget(self.readiness_badge)
        self.readiness_text=QTextEdit();self.readiness_text.setReadOnly(True);self.readiness_text.setMinimumHeight(340)
        layout.addWidget(self.readiness_text,1)
        refresh=QPushButton('فحص جاهزية الصورتين');refresh.clicked.connect(self.workflow_inspect);layout.addWidget(refresh)
        self.harmonize_button=QPushButton('توحيد الحزم للتحليل');self.harmonize_button.clicked.connect(self.workflow_harmonize);layout.addWidget(self.harmonize_button)
        for i in (0,1):
            row=QHBoxLayout()
            button=QPushButton(f'تحديد نوع الحزمة — صورة {i+1}')
            button.clicked.connect(lambda _,index=i:self.workflow_declare_band(index));row.addWidget(button)
            button=QPushButton(f'Band mapping — صورة {i+1}')
            button.clicked.connect(lambda _,index=i:self.workflow_band_mapping(index));row.addWidget(button)
            layout.addLayout(row)
        self.workflow_crop_button=QPushButton('قص منطقة التداخل الجغرافي')
        self.workflow_crop_button.clicked.connect(self.workflow_crop);layout.addWidget(self.workflow_crop_button)
        self.radiometric_accept_button=QPushButton('اعتماد فحص الإضاءة دون توحيد — بقرار المستخدم')
        self.radiometric_accept_button.clicked.connect(self.workflow_accept_radiometry);layout.addWidget(self.radiometric_accept_button)
        self.readiness_next_label=QLabel('الخطوة التالية الموصى بها: فتح الصورتين');self.readiness_next_label.setWordWrap(True)
        layout.addWidget(self.readiness_next_label)
        self.readiness_next_button=QPushButton('تنفيذ الخطوة التالية');self.readiness_next_button.clicked.connect(self.workflow_next_action)
        layout.addWidget(self.readiness_next_button);page.setWidget(content)
        return page

    def _workflow_busy(self):
        return any(self._worker_running(getattr(self,n,None)) for n in
                   ('_align_worker','_compare_worker','_change_worker','_radiometric_inspect_worker','_wf_task'))

    def _workflow_assert_idle(self):
        if self._workflow_busy():raise RuntimeError('انتظر انتهاء العملية الحالية أو اطلب إيقافها أولًا.')

    def _remember_step(self):
        self._wf_undo={k:getattr(self,k,None) for k in ('a','b','aligned','H','valid_mask','analysis_valid_mask','diff','mask',
                      'regions','change_objects','last_alignment_report','last_change_stats','compare_meta')}
        self._wf_undo['workflow']=copy.deepcopy(dict(info=self._wf_info,originals=self._wf_originals,
                            declarations=self._wf_declarations,mode=self._wf_mode,rad=self._wf_radiometric,
                            accepted=self._wf_rad_accepted))

    def _workflow_before_load(self,index,path):
        if not self._wf_internal:
            self._workflow_assert_idle();self._remember_step()
            self._wf_originals[index]=str(Path(path).resolve());self._wf_mode=None
        self._wf_radiometric=None;self._wf_rad_accepted=False

    def _workflow_after_load(self,index):
        data=(self.a,self.b)[index]
        self._wf_info[index]=None
        try:
            self._wf_info[index]=preparation.inspect_raster(data.path,self._wf_declarations.get(str(Path(data.path).resolve())))
        except Exception as exc:
            self.log('فحص Metadata: '+str(exc))
        self._wf_stage='فحص جاهزية الصورتين';self._refresh_readiness()

    def workflow_inspect(self):
        try:
            self._workflow_assert_idle()
            for i,data in enumerate((self.a,self.b)):
                if data:self._wf_info[i]=preparation.inspect_raster(data.path,self._wf_declarations.get(str(Path(data.path).resolve())))
            self._refresh_readiness();self.right_tabs.setCurrentIndex(0)
            self._save_report('فحص جاهزية الصورتين','نجاح',self.readiness_text.toPlainText())
        except Exception as exc:self.err(exc)

    def _refresh_readiness(self):
        if not hasattr(self,'readiness_text'):return
        self._wf_state=preparation.readiness(*self._wf_info,self.last_alignment_report,self._wf_radiometric,
                                             self._wf_rad_accepted,self._wf_mode)
        s=self._wf_state;status=s['status'];busy=self._workflow_busy()
        prefix={'READY':'✅','NEEDS PROCESSING':'⚠','NOT READY':'❌'}[status]
        self.readiness_badge.setText(f"{prefix} {status}\n{s['reason']}")
        self.readiness_badge.setStyleSheet('font-weight:700;padding:8px;color:'+{'READY':'#6ee7b7','NEEDS PROCESSING':'#fbbf24','NOT READY':'#ff7777'}[status])
        a,b=self._wf_info;lines=[]
        for key,label in [('crs','CRS'),('epsg','EPSG'),('origin','Coordinates / Origin'),('transform','GeoTransform'),
                          ('pixel_size','Pixel Size X/Y'),('bounds','Extent / Bounds'),('rotation_skew','Rotation / Skew'),
                          ('axis_direction','Axis direction'),('gcps','GCPs'),('gcp_crs','GCP CRS'),('sidecars','World File / Sidecar metadata')]:
            lines.append(f'{label}: 1={a.get(key) if a else "—"} | 2={b.get(key) if b else "—"}')
        equal=bool(a and b and a['crs']==b['crs'] and a['transform']==b['transform'] and (a['width'],a['height'])==(b['width'],b['height']))
        lines.append('Grid: '+('READY' if equal else 'غير متطابقة / غير متاحة'))
        ov=s.get('overlap')
        lines.append('Overlap: '+(f"Image 1={ov['percent_a']:.2f}% | Image 2={ov['percent_b']:.2f}%" if ov else 'غير متاح'))
        if ov and min(ov['percent_a'],ov['percent_b'])<99.999 and ov['exists']:
            lines.append('التداخل جزئي؛ يوصى بقص منطقة التداخل عند الاقتصار على المنطقة المشتركة.')
        for i,info in enumerate((a,b),1):
            for key,label in [('bands','Bands'),('band_type','Band Type'),('band_types','Band semantics'),('rgb','RGB mapping'),
                              ('bit_depth','Bit Depth'),('dynamic_range','Dynamic Range P2/P50/P98'),('width','Width'),('height','Height'),
                              ('nodata','NoData'),('black_percent','Black Borders %'),('valid_percent','Valid data %')]:
                lines.append(f'{label} Image {i}: {info.get(key) if info else "—"}')
        lines.append('Band Compatibility: '+s['compatibility'].get('status','—'))
        lines.append('Analysis Mode: '+s.get('analysis_mode','—'))
        r=self.last_alignment_report or {}
        lines.append('Geometric Alignment: '+('READY' if r.get('alignment_approved') else 'لم تعتمد'))
        for key,label in [('shift_x_px','Shift X'),('shift_y_px','Shift Y'),('rotation_deg','Rotation'),('scale','Scale'),
                          ('matches','Matches'),('inlier_ratio','Inliers ratio'),('reprojection_error_px','Reprojection Error'),
                          ('coverage_percent','Inlier Coverage %'),('overlap_percent','Valid Overlap %'),('ecc_score','ECC'),
                          ('residual_median_px','Residual Median'),('residual_p90_px','Residual P90'),('local_affine_passes','Local Residual'),
                          ('smooth_mesh_passes','Smooth Mesh')]:lines.append(f'{label}: {r.get(key,"—")}')
        rad=self._wf_radiometric or {}
        structural=s['compatibility'].get('mode')=='STRUCTURAL COMPARISON'
        lines.append('Radiometric: '+('غير مطبق — مقارنة بنيوية فقط' if structural else rad.get('recommendation','لم يفحص')))
        for key,label in [('brightness_difference_percent','Brightness Difference %'),('contrast_difference_percent','Contrast Difference %'),
                          ('color_difference_percent','Color Difference %'),('histogram_difference_percent','Histogram Difference %')]:
            lines.append(f'{label}: {rad.get(key,"—")}')
        lines.extend([f'Original Image 1: {self._wf_originals[0]}',f'Original Image 2: {self._wf_originals[1]}',
                      f'Final Status: {prefix} {status}'])
        self.readiness_text.setPlainText('\n'.join(lines))
        next_text={'open':'فتح الصورتين','bands':'تحديد نوع الحزم / Band mapping','reproject':'إصلاح CRS / Reproject',
                   'crop':'قص منطقة التداخل الجغرافي',
                   'metadata_copy':'إنشاء نسخة بإحداثيات Sidecar الموثقة',
                   'grid':'توحيد الصورة 2 على شبكة الصورة 1','harmonize':'توحيد الحزم للتحليل','align':'المواءمة الهندسية',
                   'inspect_colors':'فحص الإضاءة والألوان','radiometric':'توحيد الإضاءة والألوان (بقرار المستخدم)',
                   'compare':'استخراج التغيير' if self.diff is not None else 'حساب المقارنة'}.get(s['action'],'معالجة Metadata من مصدر موثوق')
        for label in (self.workflow_next,self.readiness_next_label):label.setText('الخطوة التالية الموصى بها: '+next_text)
        self.workflow_current.setText('الخطوة الحالية: '+self._wf_stage)
        for button in (self.workflow_next_button,self.readiness_next_button):button.setText(next_text);button.setEnabled(bool(s['action']) and not busy)
        self.harmonize_button.setEnabled(s['compatibility'].get('status')=='NEEDS HARMONIZATION' and not busy)
        self.workflow_crop_button.setEnabled(bool(ov and ov['exists'] and min(ov['percent_a'],ov['percent_b'])<99.999) and not busy)
        self.radiometric_accept_button.setEnabled(bool(rad.get('recommend_match')) and not self._wf_rad_accepted and not busy)
        if hasattr(self,'undo_action'):self.undo_action.setEnabled(self._wf_undo is not None and not busy)
        if hasattr(self,'stop_action'):self.stop_action.setEnabled(busy)
        for name in ('method','threshold','threshold_spin','min_area','kernel','edge_tolerance',
                     'accept_conf','review_conf','merge_distance','align_ratio'):
            widget=getattr(self,name,None)
            if widget is not None:widget.setEnabled(not busy)
        self.workflow_sequence_button.setEnabled(not busy and not self._wf_sequence and s['action'] in ('metadata_copy','crop','grid','reproject','harmonize','align','inspect_colors'))
        if structural:self.method.setCurrentText('STRUCTURAL')
        elif self.method.currentText()=='STRUCTURAL':self.method.setCurrentText('ROBUST FINAL')
        self.method.setEnabled(not busy and not structural)
        self.metric_change.t.setText('نسبة المرشحات البنيوية' if structural else 'نسبة التغيير الحقيقي')
        self.metric_area.t.setText('مساحة المرشحات البنيوية' if structural else 'مساحة التغيير الحقيقي')
        if structural:self.results_source_label.setText('النتائج الحالية: أوصاف بنيوية — المرشحات تحتاج مراجعة')
        for name in ('gis_inspect_colors_button','gis_radiometric_button'):
            widget=getattr(self,name,None)
            if widget:widget.setEnabled(not busy and not structural and s['action'] in ('align','inspect_colors','radiometric','compare'))
        for widget in getattr(self,'_comparison_controls',[]):widget.setEnabled(not busy and status=='READY')
        for name in ('compare_action','detect_action'):
            widget=getattr(self,name,None)
            if widget:widget.setEnabled(not busy and status=='READY')
        for name in ('review_result_button','delete_result_button'):
            widget=getattr(self,name,None)
            if widget:widget.setEnabled(not busy and bool(self.regions))

    def workflow_sequence_start(self):
        self._workflow_assert_idle();self._wf_sequence=True;self._wf_sequence_seen=set()
        self._workflow_sequence_continue()

    def _workflow_sequence_continue(self):
        if not self._wf_sequence or self._workflow_busy():return
        self._refresh_readiness();action=self._wf_state.get('action')
        signature=(action,tuple(m['path'] if m else '' for m in self._wf_info))
        if action not in ('metadata_copy','crop','grid','reproject','harmonize','align','inspect_colors') or signature in self._wf_sequence_seen:
            self._wf_sequence=False
            self._wf_stage='انتهى التسلسل: '+self._wf_state['reason'];self._refresh_readiness();return
        self._wf_sequence_seen.add(signature);self.workflow_next_action()
        if not self._workflow_busy():
            self._wf_sequence=False;self._refresh_readiness()

    def _workflow_radiometric_gate(self):
        self._workflow_alignment_gate()
        if self._wf_state['compatibility'].get('mode')=='STRUCTURAL COMPARISON':
            raise RuntimeError('RGB ↔ NIR: التوحيد الإشعاعي وفحص توزيع الشدة العادي غير صالحين؛ استخدم المقارنة البنيوية.')

    def workflow_next_action(self):
        action=self._wf_state.get('action')
        handlers={'open':self.open_a if self.a is None else self.open_b,'bands':lambda:self.workflow_declare_band(0 if self._wf_info[0]['band_type']=='Unknown' else 1),
                  'crop':self.workflow_crop,
                  'metadata_copy':self.workflow_metadata_copy,
                  'grid':self.workflow_grid,'reproject':self.workflow_grid,'harmonize':self.workflow_harmonize,
                  'align':self.align,'inspect_colors':self.gis_inspect_radiometry,'radiometric':self.workflow_radiometric,
                  'compare':self.detect_changes if self.diff is not None else self.run_compare}
        if action in handlers:handlers[action]()

    def _workflow_alignment_gate(self):
        self._refresh_readiness()
        if self._wf_state.get('action') not in ('align','inspect_colors','radiometric','compare'):
            raise RuntimeError(self._wf_state['reason'])

    def _workflow_compare_gate(self):
        self._refresh_readiness()
        if self._wf_state['status']!='READY':raise RuntimeError(self._wf_state['reason'])
        structural=self._wf_state['compatibility'].get('mode')=='STRUCTURAL COMPARISON'
        if (self.method.currentText()=='STRUCTURAL') != structural:raise RuntimeError('طريقة المقارنة غير متوافقة مع الحزم الحالية.')

    def workflow_declare_band(self,index):
        try:
            self._workflow_assert_idle();info=self._wf_info[index]
            if not info:raise RuntimeError('افتح الصورة أولًا.')
            if info['bands']!=1:
                self.workflow_band_mapping(index);return
            if info['band_type'] not in ('Unknown','Other') and not info['user_declared']:
                raise RuntimeError('نوع الحزمة معروف من Metadata؛ لا يُستبدل لتجاوز عدم التوافق.')
            value,ok=QInputDialog.getItem(self,'نوع الحزمة','اختيار المستخدم فقط؛ لا يغيّر Metadata الأصلية',
                                        ['Grayscale','Panchromatic','NIR','Other'],0,False)
            if not ok:return
            self._remember_step();self._wf_declarations[info['path']]={'type':value}
            self._invalidate_registration();self._wf_radiometric=None;self.workflow_inspect()
        except Exception as exc:self.err(exc)

    def workflow_band_mapping(self,index):
        try:
            self._workflow_assert_idle();info=self._wf_info[index]
            if not info or info['bands']<3:raise RuntimeError('RGB mapping يحتاج ثلاث حزم على الأقل.')
            if info['band_type']=='DEM':raise RuntimeError('لا يمكن تحويل DEM إلى RGB بتغيير التسمية.')
            value,ok=QInputDialog.getText(self,'Band mapping required','أرقام Red,Green,Blue من مصدر موثوق؛ مثال: 3,2,1')
            if not ok:return
            mapping=[int(x.strip()) for x in value.split(',')]
            preparation.inspect_raster(info['path'],{'rgb':mapping})
            self._remember_step();self._wf_declarations[info['path']]={'rgb':mapping}
            self._invalidate_registration();self._wf_radiometric=None;self.workflow_inspect()
        except Exception as exc:self.err(exc)

    def _new_analysis_path(self,name):
        folder=runtime_paths.get_storage_path('processed_images')/'analysis_work'/time.strftime('%Y%m%d_%H%M%S')
        validate_storage_path(folder,create=True)
        return str(folder/(name+'_'+str(time.time_ns())+'.tif'))

    def _protect_originals(self,*outputs):
        protected={os.path.normcase(os.path.realpath(p)) for p in self._wf_originals if p}
        protected.update(os.path.normcase(os.path.realpath(d.path)) for d in (self.a,self.b) if d)
        if any(os.path.normcase(os.path.realpath(p)) in protected for p in outputs):
            raise ValueError('لا يمكن حفظ أي معالجة فوق الصور الأصلية أو المدخلات الحالية.')

    def workflow_metadata_copy(self):
        try:
            self._workflow_assert_idle()
            info=copy.deepcopy(self._wf_info)
            outputs=[self._new_analysis_path('metadata_'+str(i+1)) if m.get('recovered_georef') else None for i,m in enumerate(info)]
            def job(w):
                for i,path in enumerate(outputs):
                    if path:preparation.copy_georef(info[i]['path'],path,info[i],w.isInterruptionRequested)
                return {'outputs':outputs}
            self._start_preparation('تضمين الإحداثيات الموثقة في نسخ جديدة',job,
                                    lambda r:self.workflow_adopt_result(r['outputs']))
        except Exception as exc:self.err(exc)

    def _start_preparation(self,name,job,adopt):
        self._workflow_assert_idle();self._remember_step();self._wf_stage=name
        self._wf_operation_name=name
        self.task_progress.setRange(0,0);self.task_progress.setVisible(True)
        self._wf_adopt=adopt;worker=PreparationWorker(job,self);self._wf_task=worker
        worker.progress.connect(self._align_progress)
        worker.completed.connect(self._preparation_done);worker.failed.connect(self._preparation_failed)
        worker.finished.connect(self._preparation_finished);worker.start();self._refresh_readiness()

    def _preparation_done(self,result):
        try:
            self._wf_adopt(result);self._save_report(self._wf_operation_name,'نجاح',json.dumps(result,ensure_ascii=False))
        except Exception as exc:
            self._wf_sequence=False;self.err(exc)

    def _preparation_failed(self,text):
        self._wf_sequence=False
        self._wf_stage='توقفت المعالجة — لم تعتمد نتيجة جديدة';self.log(text)
        if not text.startswith('تم إيقاف'):self.err(text)

    def _preparation_finished(self):
        self._dispose_worker('_wf_task');self._wf_adopt=None;self.task_progress.setRange(0,100);self.task_progress.setVisible(False);self._refresh_readiness()

    def workflow_adopt_result(self,paths,mode=None):
        self._wf_internal=True
        try:
            if paths[0]:self._load_a(paths[0])
            if paths[1]:self._load_b(paths[1])
            if mode:self._wf_mode=mode
        finally:self._wf_internal=False
        self._wf_stage='إعادة فحص النتائج المشتقة';self._refresh_readiness()

    def workflow_grid(self):
        try:
            self.need();self._refresh_readiness()
            if self._wf_state['action'] not in ('grid','reproject'):
                self.log('لا حاجة لتوحيد الشبكة أو توجد متطلبات سابقة: '+self._wf_state['reason']);return
            import gis_tools
            a,b=self.a.path,self.b.path;out=self._new_analysis_path('grid_after')
            processor=str(self.gis_processor.currentData() or 'cpu')
            self._start_preparation('توحيد الشبكة / Reproject',
                lambda w:gis_tools.align_to_reference(a,b,out,processor=processor,cancel=w.isInterruptionRequested),
                lambda r:self.workflow_adopt_result([None,r['output']]))
        except Exception as exc:self.err(exc)

    def workflow_harmonize(self):
        try:
            self.need();self._refresh_readiness()
            if self._wf_state['action']!='harmonize':raise RuntimeError('أكمل متطلبات الشبكة والتوافق أولًا: '+self._wf_state['reason'])
            a,b=self.a.path,self.b.path;ia,ib=copy.deepcopy(self._wf_info)
            oa,ob=self._new_analysis_path('analysis_before'),self._new_analysis_path('analysis_after')
            self._start_preparation('توحيد الحزم للتحليل',
                lambda w:preparation.harmonize_pair(a,b,oa,ob,ia,ib,w.isInterruptionRequested,w.report),
                lambda r:self.workflow_adopt_result(r['outputs'],r['mode']))
        except Exception as exc:self.err(exc)

    def workflow_crop(self):
        try:
            self.need();self._refresh_readiness();ov=self._wf_state.get('overlap')
            if not ov or not ov['exists'] or min(ov['percent_a'],ov['percent_b'])>=99.999:
                raise RuntimeError('القص غير مطلوب أو التداخل غير متاح.')
            import gis_tools
            a,b=self.a.path,self.b.path;oa,ob=self._new_analysis_path('overlap_before'),self._new_analysis_path('overlap_after')
            def job(w):
                w.report(10,'قص منطقة التداخل...');return preparation.crop_pair(a,b,oa,ob,w.isInterruptionRequested)
            self._start_preparation('قص منطقة التداخل الجغرافي',job,
                                    lambda r:self.workflow_adopt_result([r['before_overlap'],r['after_overlap']]))
        except Exception as exc:self.err(exc)

    def workflow_advanced_grid(self):
        try:
            self.need();self._workflow_assert_idle();self._refresh_readiness()
            if self._wf_state['action'] not in ('grid','reproject'):
                self.gis_status.setText('التوحيد غير مطلوب أو توجد متطلبات سابقة: '+self._wf_state['reason']);return
            suggested=str(get_storage_path('geotiff')/'after_aligned_to_before.tif')
            out,_=QFileDialog.getSaveFileName(self,'حفظ الصورة 2 على شبكة الصورة 1',suggested,'GeoTIFF (*.tif *.tiff)')
            if not out:return
            if not out.lower().endswith(('.tif','.tiff')):out+='.tif'
            if not confirm_overwrite(self,out):return
            validate_storage_path(Path(out).parent,create=True)
            self._protect_originals(out)
            import gis_tools
            a,b=self.a.path,self.b.path;processor=str(self.gis_processor.currentData() or 'cpu')
            staged=stage_output_path(out)
            def job(w):
                try:
                    result=gis_tools.align_to_reference(a,b,str(staged),processor=processor,cancel=w.isInterruptionRequested)
                    commit_staged_output(staged,out,overwrite=True)
                    result['output']=out
                    return result
                finally:
                    staged.unlink(missing_ok=True)
            self._start_preparation('GIS توحيد الشبكة',job,
                                    lambda r:self.workflow_adopt_result([None,r['output']]))
        except Exception as exc:self.err(exc)

    def workflow_advanced_crop(self):
        try:
            self.need();self._workflow_assert_idle()
            folder=QFileDialog.getExistingDirectory(self,'مجلد حفظ منطقة التداخل',str(get_storage_path('gis')))
            if not folder:return
            stamp=time.strftime('%Y%m%d_%H%M%S')
            result_folder=Path(folder)/f'Overlap_{stamp}'
            validate_storage_path(folder,create=True)
            result_folder.mkdir(parents=True,exist_ok=False)
            oa,ob=str(result_folder/'before_overlap.tif'),str(result_folder/'after_overlap.tif')
            self._protect_originals(oa,ob);a,b=self.a.path,self.b.path
            sa,sb=stage_output_path(oa),stage_output_path(ob)
            def adopt(r):
                self._gis_overlap_reference_path=r['before_overlap'];self._gis_overlap_moving_path=r['after_overlap']
                self.workflow_adopt_result([r['before_overlap'],r['after_overlap']])
            def job(w):
                try:
                    result=preparation.crop_pair(a,b,str(sa),str(sb),w.isInterruptionRequested)
                    commit_staged_output(sa,oa)
                    commit_staged_output(sb,ob)
                    result['before_overlap']=oa;result['after_overlap']=ob
                    return result
                finally:
                    sa.unlink(missing_ok=True);sb.unlink(missing_ok=True)
            self._start_preparation('GIS قص التداخل',job,adopt)
        except Exception as exc:self.err(exc)

    def workflow_advanced_radiometric(self):
        try:
            self.need();self._workflow_assert_idle();self._workflow_radiometric_gate()
            suggested=str(get_storage_path('geotiff')/'after_radiometric_matched.tif')
            out,_=QFileDialog.getSaveFileName(self,'حفظ الصورة 2 بعد توحيد الإضاءة والألوان',suggested,'GeoTIFF (*.tif *.tiff)')
            if not out:return
            if not out.lower().endswith(('.tif','.tiff')):out+='.tif'
            if not confirm_overwrite(self,out):return
            validate_storage_path(Path(out).parent,create=True)
            self._protect_originals(out)
            import gis_tools
            a,b=self.a.path,self.b.path
            staged=stage_output_path(out)
            def job(w):
                try:
                    result=gis_tools.radiometric_match(a,b,str(staged),cancel=w.isInterruptionRequested)
                    commit_staged_output(staged,out,overwrite=True)
                    result['output']=out
                    return result
                finally:
                    staged.unlink(missing_ok=True)
            self._start_preparation('GIS توحيد الإضاءة والألوان',job,
                                    lambda r:self.workflow_adopt_result([None,r['output']]))
        except Exception as exc:self.err(exc)

    def workflow_radiometric(self):
        try:
            self.need();self._refresh_readiness()
            if self._wf_state['action']!='radiometric':raise RuntimeError('نفّذ فحص الإضاءة أولًا؛ لا تطابق Histogram دون توصية.')
            import gis_tools
            a,b=self.a.path,self.b.path;out=self._new_analysis_path('radiometric_after')
            self._start_preparation('توحيد الإضاءة والألوان',
                lambda w:gis_tools.radiometric_match(a,b,out,cancel=w.isInterruptionRequested),
                lambda r:self.workflow_adopt_result([None,r['output']]))
        except Exception as exc:self.err(exc)

    def workflow_accept_radiometry(self):
        if self._wf_radiometric and not self._workflow_busy():
            self._remember_step();self._wf_rad_accepted=True;self._refresh_readiness()
            self._save_report('اعتماد فحص الإضاءة دون توحيد','قرار المستخدم',self._wf_radiometric.get('recommendation',''))

    def workflow_undo(self):
        try:
            self._workflow_assert_idle()
            if not self._wf_undo:return
            state=self._wf_undo;self._wf_undo=None;wf=state.pop('workflow')
            for key,value in state.items():setattr(self,key,value)
            self._wf_info=wf['info'];self._wf_originals=wf['originals'];self._wf_declarations=wf['declarations']
            self._wf_mode=wf['mode'];self._wf_radiometric=wf['rad'];self._wf_rad_accepted=wf['accepted']
            self.va.set_array(self.a.array if self.a else None);self.vb.set_array(self.current_b() if self.b else None)
            self.vr.set_array(None);self._fill_regions();self._fill_objects()
            if self.a:self._show_meta();self.refresh_result_view()
            self._wf_stage='رجوع خطوة';self._refresh_readiness()
            self._save_report('رجوع خطوة','نجاح','استعادة الحالة السابقة دون حذف الملفات المشتقة أو تغيير الأصول.')
        except Exception as exc:self.err(exc)

    def workflow_stop(self):
        self._wf_sequence=False
        for name in ('_align_worker','_compare_worker','_change_worker','_radiometric_inspect_worker','_wf_task'):
            w=getattr(self,name,None)
            if self._worker_running(w):w.requestInterruption()
        self._wf_stage='طلب إيقاف — انتظار انتهاء المرحلة الحالية بأمان';self._refresh_readiness()

    def workflow_project_state(self):
        return dict(originals=self._wf_originals,declarations=self._wf_declarations,analysis_mode=self._wf_mode)

    def workflow_review_result(self):
        try:
            self._workflow_assert_idle();row=self.table.currentRow()
            if not 0<=row<len(self.regions):raise ValueError('حدد نتيجة من جدول المناطق أولًا.')
            decision,ok=QInputDialog.getItem(self,'مراجعة النتيجة','قرار المراجع',['يحتاج مراجعة','تمت المراجعة — مقبول'],0,False)
            if not ok:return
            self._remember_step();self.regions=copy.deepcopy(self.regions)
            self.regions[row]['classification']=decision;self.regions[row]['review_status']=decision
            self._fill_regions();self.table.selectRow(row)
            self._save_report('مراجعة نتيجة','قرار المستخدم',json.dumps(self.regions[row],ensure_ascii=False))
        except Exception as exc:self.err(exc)

    def workflow_delete_result(self):
        try:
            import cv2
            import engine
            self._workflow_assert_idle();row=self.table.currentRow()
            if self.mask is None or not 0<=row<len(self.regions):raise ValueError('حدد نتيجة من جدول المناطق أولًا.')
            region=self.regions[row];x,y,w,h=[int(region[k]) for k in ('x','y','width','height')]
            local=self.mask[y:y+h,x:x+w]
            count,labels,stats,_=cv2.connectedComponentsWithStats((local>0).astype(np.uint8),8)
            matches=[i for i in range(1,count) if tuple(stats[i,:4])==(0,0,w,h) and stats[i,4]==region['area_pixels']]
            if len(matches)!=1:raise ValueError('تعذر تحديد قناع النتيجة منفردًا؛ أعد استخراج المناطق قبل الاستبعاد.')
            self._remember_step();self.mask=self.mask.copy()
            self.mask[y:y+h,x:x+w][labels==matches[0]]=0
            self.regions=[dict(r) for i,r in enumerate(self.regions) if i!=row]
            # Recompute geometry/counts from the edited mask; never retain a
            # polygon or group count that includes the excluded component.
            self.change_objects=[]
            self.last_change_stats=engine.stats(self.mask,self.a,self.diff,valid_mask=self.analysis_valid_mask)
            self.last_change_stats.update(object_count=0,manual_exclusion=True,remaining_regions=len(self.regions))
            self._fill_regions();self._fill_objects();self.refresh_result_view()
            self.va.set_array(self.a.array);self.vb.set_array(self.current_b())
            self.metric_change.set_value(f"{self.last_change_stats['changed_percent']:.2f}%")
            self.metric_regions.set_value(str(len(self.regions)))
            for name in ('metric_objects','metric_appear','metric_disappear','metric_stable','metric_structural','metric_review'):getattr(self,name).set_value('—')
            self.stats_detail.setPlainText(json.dumps(self.last_change_stats,ensure_ascii=False,indent=2))
            self._refresh_readiness();self._save_report('استبعاد نتيجة','قرار المستخدم',json.dumps(region,ensure_ascii=False))
        except Exception as exc:self.err(exc)

    def _workflow_view_geometry(self,view):
        return self.b if view is self.vb and self.aligned is None else self.a

    def _workflow_map_point(self,source,target,x,y):
        a,b=self._workflow_view_geometry(source),self._workflow_view_geometry(target)
        if a is None or b is None:return None
        if a is b:return float(x),float(y)
        if a.crs is None or b.crs is None or a.transform is None or b.transform is None:return None
        wx,wy=a.transform*(float(x),float(y))
        if a.crs!=b.crs:
            from pyproj import Transformer
            wx,wy=Transformer.from_crs(a.crs,b.crs,always_xy=True).transform(wx,wy,errcheck=True)
        return (~b.transform)*(wx,wy)

    def _workflow_sync_views(self,source,state):
        if not self._sync_enabled() or not state or self._wf_syncing:return
        self._wf_syncing=True
        try:
            x,y=state.get('cx'),state.get('cy')
            if x is None:return
            for target in (self.va,self.vb,self.vr):
                if target is source:continue
                p=self._workflow_map_point(source,target,x,y)
                q=self._workflow_map_point(source,target,x+1,y)
                r=self._workflow_map_point(source,target,x,y+1)
                if p is None:continue
                determinant=(q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
                scale=state['scene_scale']/max(math.sqrt(abs(determinant)),1e-12)
                target.apply_view_state(dict(cx=p[0],cy=p[1],scene_scale=scale))
        except Exception as exc:self.statusBar().showMessage('تعذر مزامنة الإحداثيات: '+str(exc),4000)
        finally:self._wf_syncing=False

    def workflow_full_view(self,box):
        boxes=(self.before_box,self.after_box,self.change_box)
        if self._wf_full_view is box:
            for b in boxes:b.show()
            self.viewer_splitter.setSizes(getattr(self,'_wf_split_sizes',[1,1,1]));self._wf_full_view=None
        else:
            if self._wf_full_view is None:self._wf_split_sizes=self.viewer_splitter.sizes()
            for b in boxes:b.setVisible(b is box)
            self._wf_full_view=box
        for b in boxes:b.full_view_button.setText('العودة للوضع الثلاثي' if b is self._wf_full_view else 'Full View')

    def workflow_select_at(self,x,y):
        candidates=[(i,r) for i,r in enumerate(self.regions) if r.get('x',0)<=x<r.get('x',0)+r.get('width',0)
                    and r.get('y',0)<=y<r.get('y',0)+r.get('height',0)]
        if candidates:
            i,r=min(candidates,key=lambda pair:pair[1].get('area_pixels',float('inf')))
            self.table.selectRow(i);self.workflow_focus_region(r,i)

    def workflow_focus_region(self,item,row=0):
        if self.a is None or not ('cx' in item or 'x' in item):return False
        x=float(item.get('cx',item.get('x',0)+item.get('width',0)/2))
        y=float(item.get('cy',item.get('y',0)+item.get('height',0)/2))
        for view,marker in getattr(self,'_wf_markers',[]):
            try:view.scene_obj.removeItem(marker)
            except RuntimeError:pass
        self._wf_markers=[]
        pen=QPen(QColor('#00ffff'));pen.setCosmetic(True);pen.setWidth(2)
        for view in (self.va,self.vb,self.vr):
            p=self._workflow_map_point(self.va,view,x+.5,y+.5)
            if p is None:continue
            view.apply_view_state(dict(cx=p[0],cy=p[1],scene_scale=3.5))
            for marker in (view.scene_obj.addEllipse(p[0]-3,p[1]-3,6,6,pen),
                           view.scene_obj.addLine(p[0]-8,p[1],p[0]+8,p[1],pen),
                           view.scene_obj.addLine(p[0],p[1]-8,p[0],p[1]+8,pen)):
                marker.setZValue(100);self._wf_markers.append((view,marker))
        self._refresh_native_detail_views();self.statusBar().showMessage(f'النتيجة {item.get("id",row+1)} — Pixel ({x:.1f}, {y:.1f})',5000)
        return True
