"""Independent Arabic report editor. No production settings or analysis writes."""
from __future__ import annotations
import copy
import io
import logging
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QThread,Signal,QPointF,QRectF
from PySide6.QtGui import QPainter,QColor,QFont
from PySide6.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QSplitter,QTreeWidget,QTreeWidgetItem,
 QPlainTextEdit,QLineEdit,QLabel,QPushButton,QMessageBox,QFileDialog,QInputDialog,QComboBox,QSpinBox,
 QDoubleSpinBox,QCheckBox,QFormLayout,QTableWidget,QTableWidgetItem,QAbstractScrollArea,QFontComboBox,QColorDialog)
from report_document import ReportDocument,ReportController,block,uid,report_target_name_column
from report_assets import AssetStore
from report_adapter import from_snapshot
from report_project import save_project,load_project
from report_layout import ReportLayout,WIDTH,HEIGHT,image_payload,display
from report_cancel import CancellationToken,ReportCancelled,cancellation_scope,checkpoint
from storage_manager import get_storage_path,validate_storage_path

class Task(QThread):
    done=Signal(object)
    error=Signal(str)
    progress=Signal(object)
    cancelled=Signal()
    def __init__(self,fn,parent=None,with_progress=False):super().__init__(parent);self.fn=fn;self.with_progress=with_progress;self.cancel_token=CancellationToken()
    def requestInterruption(self):
        accepted=self.cancel_token.cancel()
        if accepted:super().requestInterruption()
        return accepted
    def run(self):
        try:
            with cancellation_scope(self.cancel_token):
                checkpoint();result=self.fn(self.progress.emit) if self.with_progress else self.fn();self.cancel_token.finish()
            self.done.emit(result)
        except ReportCancelled:self.cancelled.emit()
        except Exception as exc:
            logging.getLogger(__name__).exception('Report background operation failed')
            self.error.emit(str(exc))

class PagePreview(QAbstractScrollArea):
    selected=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self.layout=None;self.partial=None;self.report_uid=None;self.zoom=.8;self.setMinimumWidth(320)
        self.verticalScrollBar().valueChanged.connect(lambda _:self.viewport().update())
    def set_layout(self,layout):self.layout=layout;self.partial=None;self.resizeEvent(None);self.viewport().update()
    def set_partial(self,image):self.layout=None;self.partial=image;self.resizeEvent(None);self.viewport().update()
    def resizeEvent(self,event):
        count=self.layout.page_count if self.layout else 1 if self.partial is not None else 0
        self.verticalScrollBar().setRange(0,max(0,int(count*(HEIGHT*self.zoom+20)-self.viewport().height())))
        self.verticalScrollBar().setPageStep(self.viewport().height())
        if event:super().resizeEvent(event)
    def paintEvent(self,event):
        p=QPainter(self.viewport());p.fillRect(self.viewport().rect(),QColor('#ccd5dc'))
        if not self.layout:
            if self.partial is not None:p.drawImage(QRectF(10,10-self.verticalScrollBar().value(),WIDTH*self.zoom,HEIGHT*self.zoom),self.partial)
            return
        scroll=self.verticalScrollBar().value();step=HEIGHT*self.zoom+20
        for page in range(max(0,int(scroll/step)),min(self.layout.page_count,int((scroll+self.viewport().height())/step)+1)):
            p.save();p.translate(10,page*step-scroll+10);p.scale(self.zoom,self.zoom);self.layout.draw_page(p,page);p.restore()
    def mousePressEvent(self,event):
        if not self.layout:return
        y=event.position().y()+self.verticalScrollBar().value()-10;step=HEIGHT*self.zoom+20;page=int(y/step)
        if 0<=page<self.layout.page_count:
            key=self.layout.hit(page,QPointF((event.position().x()-10)/self.zoom,(y-page*step)/self.zoom))
            if key:self.selected.emit(key)

class ReportEditorDialog(QDialog):
    def __init__(self,snapshot=None,parent=None):
        super().__init__(parent);self.setWindowTitle('محرر التقارير — ChangeVision 2026');self.setLayoutDirection(Qt.RightToLeft);self.resize(1460,900)
        self.setWindowFlags(self.windowFlags() | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.controller=ReportController();self.assets=AssetStore();self.path=None;self.jobs=[];self.generation=0;self.loading=False;self.selected_uid=None;self.form_dirty=False;self.form_loading=False;self.tree_generation=0;self.tree_pending=0;self.load_generation=0
        root=QVBoxLayout(self);filebar=QHBoxLayout();root.addLayout(filebar)
        for label,fn in [('فتح مشروع تقرير',self.open_file),('حفظ',self.save),('حفظ باسم',lambda:self.save(True)),('تراجع',self.undo),('إعادة',self.redo),('تصدير Word',lambda:self.export('docx')),('تصدير PDF',lambda:self.export('pdf'))]:self.button(filebar,label,fn)
        self.cancel_button=QPushButton('إلغاء العملية');self.cancel_button.setEnabled(False);self.cancel_button.clicked.connect(self.cancel_operations);filebar.addWidget(self.cancel_button)
        split=QSplitter();root.addWidget(split,1)
        left=QWidget();ll=QVBoxLayout(left);self.tree=QTreeWidget();self.tree.setHeaderLabels(['الأقسام والعناصر — المخفي يبقى ظاهرًا هنا']);ll.addWidget(self.tree);split.addWidget(left)
        self.tree.currentItemChanged.connect(self.select)
        for labels in [[('قسم جديد',self.add_section),('حذف من التقرير',self.delete),('إخفاء/إظهار',self.toggle)], [('أعلى',lambda:self.move(-1)),('أسفل',lambda:self.move(1)),('استعادة الأقسام',self.restore)]]:
            row=QHBoxLayout();ll.addLayout(row)
            for title,fn in labels:self.button(row,title,fn)
        row=QHBoxLayout();ll.addLayout(row);self.kind=QComboBox();self.kind.addItems(['عنوان','فقرة','ملاحظة','استنتاج','حقل','جدول','صورة']);row.addWidget(self.kind);self.button(row,'إضافة عنصر',self.add_item)
        self.section_dest=QComboBox();ll.addWidget(self.section_dest);self.button(ll,'نقل العنصر إلى القسم',self.move_to_section)
        self.button(ll,'إعادة ضبط التصميم دون حذف المحتوى',self.reset_design)
        form=QWidget();fl=QVBoxLayout(form);self.label=QLineEdit();self.content=QPlainTextEdit();self.content.setMaximumHeight(180);fl.addWidget(QLabel('العنوان / اسم الحقل / تعليق الصورة'));fl.addWidget(self.label);fl.addWidget(self.content)
        styles=QFormLayout();fl.addLayout(styles);self.font=QFontComboBox();styles.addRow('الخط',self.font);self.size=QSpinBox();self.size.setRange(7,48);self.size.setValue(11);styles.addRow('الحجم',self.size)
        self.bold=QCheckBox('عريض');self.italic=QCheckBox('مائل');styles.addRow(self.bold,self.italic)
        self.align=QComboBox();self.align.addItems(['right','center','left']);styles.addRow('محاذاة',self.align)
        self.color='#183c57';self.color_button=QPushButton('لون النص');self.color_button.clicked.connect(self.choose_color);styles.addRow(self.color_button)
        self.image_width=QDoubleSpinBox();self.image_width.setRange(10,176);self.image_width.setValue(140);styles.addRow('عرض الصورة mm',self.image_width)
        self.variant=QComboBox();self.variant.addItems(['original','annotated','both']);styles.addRow('نسخة الصورة',self.variant)
        self.target=QComboBox();styles.addRow('هدف الصورة اليدوية',self.target)
        self.button(fl,'تضمين / استبعاد الهدف من التقرير فقط',self.toggle_target)
        self.role=QComboBox();self.role.addItems(['manual','before','after','change']);styles.addRow('نوع الصورة',self.role)
        row=QHBoxLayout();fl.addLayout(row);self.button(row,'تطبيق',self.apply);self.button(row,'استبدال الصورة',lambda:self.import_image(True));self.button(row,'شرح الصورة',self.annotate)
        self.table=QTableWidget();fl.addWidget(self.table,1)
        row=QHBoxLayout();fl.addLayout(row)
        for title,fn in [('صف +',self.add_row),('صف −',self.remove_row),('عمود +',self.add_column),('عمود −',self.remove_column)]:self.button(row,title,fn)
        row=QHBoxLayout();fl.addLayout(row)
        for title,fn in [('عمود ←',lambda:self.move_column(-1)),('عمود →',lambda:self.move_column(1)),('رؤية العمود',self.toggle_column),('عرض العمود',self.column_width)]:self.button(row,title,fn)
        self.button(fl,'حفظ تعديلات الجدول',self.apply_table);split.addWidget(form)
        previewbox=QWidget();pl=QVBoxLayout(previewbox);self.zoom=QSpinBox();self.zoom.setRange(25,200);self.zoom.setValue(80);self.zoom.setSuffix('%');pl.addWidget(self.zoom)
        self.preview=PagePreview();pl.addWidget(self.preview);split.addWidget(previewbox);self.preview.selected.connect(self.select_uid)
        self.zoom.valueChanged.connect(self.zoom_changed);split.setSizes([320,450,650]);self.status=QLabel('');root.addWidget(self.status)
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(250);self.debounce.timeout.connect(self.build_preview)
        for signal in [self.label.textEdited,self.content.textChanged,self.size.valueChanged,self.bold.toggled,self.italic.toggled,self.align.currentIndexChanged,self.font.currentFontChanged,self.image_width.valueChanged,self.variant.currentIndexChanged,self.table.itemChanged]:signal.connect(self.mark_form_dirty)
        self.table.horizontalHeader().sectionDoubleClicked.connect(self.rename_column)
        self.refresh()
        if snapshot is not None:
            self.load_generation+=1;token=self.load_generation
            self.task(lambda:from_snapshot(copy.deepcopy(snapshot)),lambda result:self.loaded(result,token=token),'تجهيز نسخة مستقلة من بيانات التقرير')
    @staticmethod
    def button(layout,title,fn):
        b=QPushButton(title);b.clicked.connect(lambda _=False:fn());layout.addWidget(b);return b
    def task(self,fn,callback,message,progress=None):
        if len(self.jobs)>=2:self.status.setText('انتظر اكتمال إحدى العمليات الحالية قبل بدء عملية أخرى');return None
        self.status.setText(message);job=Task(fn,self,with_progress=progress is not None);self.jobs.append(job)
        self.cancel_button.setEnabled(True)
        if progress is not None:job.progress.connect(progress)
        def finished():
            self.jobs.remove(job);self.cancel_button.setEnabled(bool(self.jobs))
        job.done.connect(callback);job.cancelled.connect(lambda:self.status.setText('أُلغيت العملية؛ لم يُعتمد ملف جديد'));job.error.connect(lambda text:self.status.setText('تعذر الإجراء: '+text));job.finished.connect(finished);job.finished.connect(job.deleteLater);job.start();return job
    def cancel_operations(self):
        self.debounce.stop();accepted=any([job.requestInterruption() for job in list(self.jobs)])
        if accepted:
            self.generation+=1;self.status.setText('طُلب الإلغاء؛ يُنتظر توقف العامل دون تعطيل الواجهة')
        elif self.jobs:self.status.setText('العملية انتهت أو دخلت اعتماد الملف؛ تعذر قبول إلغاء متأخر')
    def loaded(self,result,token=None):
        if token is not None and token!=self.load_generation:return
        self.controller=ReportController(result[0]);self.assets=result[1];self.selected_uid=None;self.form_dirty=False;self.form_loading=True
        self.label.clear();self.content.clear();self.table.setRowCount(0);self.table.setColumnCount(0);self.form_loading=False
        self.refresh();self.status.setText('تم فتح نسخة التقرير؛ تحذيرات المصادر: '+str(self.controller.document.warning_count()))
    def action(self,fn):
        try:
            if self.form_dirty:self.commit_form()
            fn();self.refresh()
        except Exception as exc:logging.getLogger(__name__).exception('Report edit rejected');QMessageBox.warning(self,'لم ينفذ التغيير',str(exc))
    def locate(self):
        # Internal read-only views. Every edit below goes through the controller;
        # copying the entire snapshot/default sections here stalls selection on
        # large reports, and does not add protection to a read-only UI lookup.
        sections=self.controller.document.sections_for_view()
        for s in sections:
            if s['uid']==self.selected_uid:return s,None
            for b in s['blocks']:
                if b['uid']==self.selected_uid:return s,b
        return (sections[0],None) if sections else (None,None)
    def refresh(self):
        self.loading=True;self.generation+=1;self.tree_generation+=1;self.tree_pending=0;token=self.tree_generation
        self.tree.setUpdatesEnabled(False);self.tree.blockSignals(True);self.tree.clear();self.section_dest.clear();chosen_target=self.target.currentData();self.target.clear();self.target.addItem('دون هدف — صورة عامة',None)
        selection=self.controller.document._data['target_selection']
        for t in self.controller.document.targets_for_view():self.target.addItem(str(t['display_id'])+' — '+(t['record'].get('target_name') or 'غير مدخل')+(' [مستبعد]' if not selection.get(t['uid'],True) else ''),t['uid'])
        if self.target.findData(chosen_target)>=0:self.target.setCurrentIndex(self.target.findData(chosen_target))
        for s in self.controller.document.sections_for_view():
            top=QTreeWidgetItem([s['title']+(' [مخفي]' if not s['visible'] else '')]);top.setData(0,Qt.UserRole,s['uid']);self.tree.addTopLevelItem(top);self.section_dest.addItem(s['title'],s['uid'])
            if len(s['blocks'])>150:
                self.tree_pending+=1;QTimer.singleShot(0,lambda parent=top,blocks=s['blocks']:self.fill_tree_batch(parent,blocks,0,token))
            else:
                for b in s['blocks']:self.add_tree_block(top,b)
            top.setExpanded(True)
        self.tree.blockSignals(False);self.tree.setUpdatesEnabled(True);self.loading=False;self.restore_tree_selection();self.setWindowTitle('محرر التقارير'+(' *' if self.controller.dirty else '')+' — '+(self.path.name if self.path else 'غير محفوظ'));self.debounce.start()
    def add_tree_block(self,parent,b):
        label=b.get('label') or b.get('caption') or b.get('text') or b['kind']
        excluded=b.get('target_uid') and not self.controller.document._data['target_selection'].get(b['target_uid'],True)
        item=QTreeWidgetItem([str(label)[:70]+(' [مخفي]' if not b.get('visible',True) else '')+(' [هدف مستبعد]' if excluded else '')]);item.setData(0,Qt.UserRole,b['uid']);parent.addChild(item)
    def fill_tree_batch(self,parent,blocks,start,token):
        if token!=self.tree_generation:return
        self.tree.setUpdatesEnabled(False)
        for b in blocks[start:start+150]:self.add_tree_block(parent,b)
        self.tree.setUpdatesEnabled(True)
        if start+150<len(blocks):QTimer.singleShot(0,lambda:self.fill_tree_batch(parent,blocks,start+150,token))
        else:
            self.tree_pending-=1
            if not self.tree_pending:self.restore_tree_selection()
    def restore_tree_selection(self):
        if self.selected_uid and self.select_uid(self.selected_uid):return
        if not self.tree_pending and self.tree.topLevelItemCount():self.select_uid(self.tree.topLevelItem(0).data(0,Qt.UserRole))
    def select_uid(self,key):
        if not key:return False
        it=__import__('PySide6.QtWidgets',fromlist=['QTreeWidgetItemIterator']).QTreeWidgetItemIterator(self.tree)
        while it.value():
            if it.value().data(0,Qt.UserRole)==key:
                if self.tree.currentItem() is it.value():self.select(it.value())
                else:self.tree.setCurrentItem(it.value()) # currentItemChanged selects once.
                return True
            it+=1
        return False
    def select(self,item,*_):
        if self.loading or item is None:return
        if self.form_dirty:
            try:self.commit_form()
            except Exception as exc:QMessageBox.warning(self,'تعديلات غير محفوظة',str(exc));return
        self.form_loading=True
        self.selected_uid=item.data(0,Qt.UserRole);s,b=self.locate();self.table.clear();self.table.setRowCount(0);self.table.setColumnCount(0)
        obj=b or s;self.label.setText(str(obj.get('label',obj.get('caption',obj.get('title','')))))
        self.content.setPlainText(display(obj.get('value')) if b and b['kind']=='field' else str(obj.get('text',obj.get('description',''))))
        locked=bool(b and b.get('locked'));self.content.setReadOnly(locked);self.label.setReadOnly(locked)
        st=obj.get('style') or {};self.font.setCurrentFont(QFont(st.get('font','Arial')));self.size.setValue(int(st.get('size',11)));self.bold.setChecked(bool(st.get('bold')));self.italic.setChecked(bool(st.get('italic')));self.color=st.get('color','#183c57');self.align.setCurrentText(st.get('align','right'))
        if b and b['kind']=='image':
            self.image_width.setValue(b.get('width_mm',140));self.variant.setCurrentText(b.get('variant','original'))
            target_index=self.target.findData(b.get('target_uid'));self.target.setCurrentIndex(max(0,target_index))
            self.role.setCurrentText(b.get('role') if b.get('role') in ['manual','before','after','change'] else 'manual')
        if b and b['kind']=='table':self.show_table(b)
        self.form_loading=False;self.form_dirty=False
    def show_table(self,b):
        self.edit_columns=copy.deepcopy(b['columns']);self.table.setColumnCount(len(b['columns']));self.table.setRowCount(len(b['rows']));self.table.setHorizontalHeaderLabels([c.get('title',c['key'])+(' [مخفي]' if not c.get('visible',True) else '') for c in b['columns']])
        source_count=b.get('source_row_count',len(b['rows']) if b.get('locked') else 0)
        for ri,row in enumerate(b['rows']):
            for ci,value in enumerate(row):
                item=QTableWidgetItem(display(value));item.setData(Qt.UserRole,value)
                if ri<source_count and b['columns'][ci].get('locked') and not report_target_name_column(b,b['columns'][ci]):item.setFlags(item.flags()&~Qt.ItemIsEditable)
                self.table.setItem(ri,ci,item)
    def choose_color(self):
        c=QColorDialog.getColor(QColor(self.color),self)
        if c.isValid():self.color=c.name();self.mark_form_dirty()
    def mark_form_dirty(self,*_):
        if not self.loading and not self.form_loading:self.form_dirty=True;self.setWindowTitle('محرر التقارير *')
    def apply(self):
        try:self.commit_form();self.refresh()
        except Exception as exc:QMessageBox.warning(self,'تعذر اعتماد العنصر',str(exc))
    def commit_form(self):
        s,b=self.locate()
        if not s:return
        if b:
            style={'font':self.font.currentFont().family(),'size':self.size.value(),'bold':self.bold.isChecked(),'italic':self.italic.isChecked(),'color':self.color,'align':self.align.currentText()}
            values={'style':style}
            if not b.get('locked'):
                if b['kind']=='field':values.update(label=self.label.text(),value=self.content.toPlainText())
                elif b['kind']=='image':values.update(caption=self.label.text(),description=self.content.toPlainText(),width_mm=self.image_width.value(),variant=self.variant.currentText())
                elif b['kind']!='table':values['text']=self.content.toPlainText()
            self.controller.edit_block(b['uid'],values)
            if b['kind']=='table':self.controller.edit_table(b['uid'],copy.deepcopy(self.edit_columns),self.table_rows())
        else:self.controller.mutate(lambda d:next(x for x in d['sections'] if x['uid']==s['uid']).update(title=self.label.text()))
        self.form_dirty=False
    def add_section(self):
        title,ok=QInputDialog.getText(self,'قسم جديد','عنوان القسم')
        if ok and title:self.action(lambda:self.controller.add_section(title))
    def add_item(self):
        s,b=self.locate()
        if not s:self.add_section();return
        kind=['heading','paragraph','note','conclusion','field','table','image'][self.kind.currentIndex()]
        if kind=='image':self.import_image();return
        value=block(kind,text='نص جديد',locked=False)
        if kind=='field':value=block(kind,label='حقل مخصص',value='',source='user',locked=False)
        if kind=='table':value=block(kind,columns=[{'key':uid(),'title':'عمود مخصص','visible':True,'width':1,'locked':False}],rows=[['']],locked=False)
        self.action(lambda:self.controller.add_block(s['uid'],value));self.select_uid(value['uid'])
    def toggle(self):
        key=self.selected_uid
        def change(d):
            obj=next(o for s in d['sections'] for o in [s]+s['blocks'] if o['uid']==key);obj['visible']=not obj.get('visible',True)
        if key:self.action(lambda:self.controller.mutate(change))
    def delete(self):
        s,b=self.locate()
        if not s:return
        def change(d):
            if b:next(x for x in d['sections'] if x['uid']==s['uid'])['blocks'][:]=[x for x in s['blocks'] if x['uid']!=b['uid']]
            else:d['sections'][:]=[x for x in d['sections'] if x['uid']!=s['uid']]
        self.action(lambda:self.controller.mutate(change))
    def move(self,delta):
        s,b=self.locate()
        if not s:return
        def change(d):
            seq=next(x for x in d['sections'] if x['uid']==s['uid'])['blocks'] if b else d['sections'];i=next(i for i,x in enumerate(seq) if x['uid']==(b or s)['uid']);j=i+delta
            if 0<=j<len(seq):seq[i],seq[j]=seq[j],seq[i]
        self.action(lambda:self.controller.mutate(change))
    def move_to_section(self):
        s,b=self.locate();dest=self.section_dest.currentData()
        if b and dest and dest!=s['uid']:
            def change(d):
                src=next(x for x in d['sections'] if x['uid']==s['uid']);src['blocks']=[x for x in src['blocks'] if x['uid']!=b['uid']];next(x for x in d['sections'] if x['uid']==dest)['blocks'].append(b)
            self.action(lambda:self.controller.mutate(change))
    def restore(self):self.action(self.controller.restore_sections)
    def reset_design(self):
        if QMessageBox.question(self,'إعادة ضبط التصميم','استعادة التنسيق الافتراضي مع الاحتفاظ بالمحتوى والرسومات؟')==QMessageBox.Yes:self.action(self.controller.reset_design)
    def toggle_target(self):
        target=self.target.currentData()
        if target:self.action(lambda:self.controller.toggle_target(target))
    def undo(self):self.action(self.controller.undo)
    def redo(self):self.action(self.controller.redo)
    def table_rows(self):
        s,b=self.locate();source=b.get('source_row_count',len(b['rows']) if b.get('locked') else 0)
        return [[(b['rows'][r][c] if r<source and self.edit_columns[c].get('locked') and not report_target_name_column(b,self.edit_columns[c]) else self.table.item(r,c).text() if self.table.item(r,c) else '') for c in range(self.table.columnCount())] for r in range(self.table.rowCount())]
    def apply_table(self):
        s,b=self.locate()
        if b and b['kind']=='table':self.action(lambda:self.controller.edit_table(b['uid'],copy.deepcopy(self.edit_columns),self.table_rows()))
    def add_row(self):
        if self.table.columnCount():self.table.insertRow(self.table.rowCount())
    def remove_row(self):
        s,b=self.locate();row=self.table.currentRow()
        if not b or b['kind']!='table' or row<0:return
        count=b.get('source_row_count',len(b['rows']) if b.get('locked') else 0)
        if row<count:QMessageBox.warning(self,'صف محمي','لا يحذف صف تحليل أصلي');return
        self.table.removeRow(row)
    def add_column(self):
        s,b=self.locate()
        if not b or b['kind']!='table':return
        name,ok=QInputDialog.getText(self,'عمود جديد','العنوان')
        if ok:
            rows=self.table_rows();cols=copy.deepcopy(self.edit_columns);cols.append({'key':uid(),'title':name,'visible':True,'width':1,'locked':False})
            self.action(lambda:self.controller.edit_table(b['uid'],cols,[r+[''] for r in rows]))
    def remove_column(self):
        s,b=self.locate();ci=self.table.currentColumn()
        if not b or b['kind']!='table' or ci<0:return
        if self.edit_columns[ci].get('locked'):QMessageBox.warning(self,'عمود محمي','يمكن إخفاء عمود التحليل دون حذفه');return
        rows=self.table_rows();cols=copy.deepcopy(self.edit_columns);cols.pop(ci)
        self.action(lambda:self.controller.edit_table(b['uid'],cols,[r[:ci]+r[ci+1:] for r in rows]))
    def move_column(self,delta):
        s,b=self.locate();i=self.table.currentColumn();j=i+delta
        if not b or b['kind']!='table' or i<0 or not 0<=j<len(self.edit_columns):return
        cols=copy.deepcopy(self.edit_columns);rows=self.table_rows();cols[i],cols[j]=cols[j],cols[i]
        for r in rows:r[i],r[j]=r[j],r[i]
        self.action(lambda:self.controller.edit_table(b['uid'],cols,rows))
    def toggle_column(self):
        s,b=self.locate();i=self.table.currentColumn()
        if b and b['kind']=='table' and i>=0:
            cols=copy.deepcopy(self.edit_columns);cols[i]['visible']=not cols[i].get('visible',True);self.action(lambda:self.controller.edit_table(b['uid'],cols,self.table_rows()))
    def column_width(self):
        s,b=self.locate();i=self.table.currentColumn()
        if b and b['kind']=='table' and i>=0:
            width,ok=QInputDialog.getDouble(self,'عرض العمود','الوزن النسبي',float(self.edit_columns[i].get('width',1)),.1,20,1)
            if ok:
                cols=copy.deepcopy(self.edit_columns);cols[i]['width']=width;self.action(lambda:self.controller.edit_table(b['uid'],cols,self.table_rows()))
    def rename_column(self,i):
        s,b=self.locate()
        if not b or b['kind']!='table':return
        title,ok=QInputDialog.getText(self,'عنوان العمود في التقرير','العنوان',text=self.edit_columns[i].get('title',''))
        if ok:
            cols=copy.deepcopy(self.edit_columns);cols[i]['title']=title;self.action(lambda:self.controller.edit_table(b['uid'],cols,self.table_rows()))
    def import_image(self,replace=False):
        s,b=self.locate()
        if not s or (replace and (not b or b['kind']!='image')):return
        path,_=QFileDialog.getOpenFileName(self,'صورة التقرير','', 'Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff *.gif *.jp2);;All files (*)')
        if not path:return
        target=self.target.currentData();role=self.role.currentText();section_uid=s['uid']
        origin=self.controller.document.report_uid
        def work():
            store=AssetStore();a=store.ingest(path);return a,store.payloads[a['uid']]
        def done(result):
            if origin!=self.controller.document.report_uid:return
            asset,payload=result
            try:self.assets._reserve(payload,asset['dimensions'])
            except ValueError as exc:self.status.setText(str(exc));return
            def change(d):
                d['assets'][asset['uid']]=asset
                if replace:
                    item=next(x for sec in d['sections'] for x in sec['blocks'] if x['uid']==b['uid'])
                    previous=item.get('source_ref');item.update(asset_uid=asset['uid'],original_asset_uid=asset['uid'],original_available=True,composite_asset_uid=None,layer_adjustments={},source_ref=path,annotations=[],transforms=[],variant='original',role=role,source_record={'type':'manual_replacement','previous_source_ref':previous,'user_declared_role':role})
                else:next(sec for sec in d['sections'] if sec['uid']==section_uid)['blocks'].append(block('image',placement_uid=uid(),asset_uid=asset['uid'],target_uid=target,role=role,source_ref=path,caption=Path(path).name,width_mm=140,variant='original',annotations=[]))
            self.assets.payloads[asset['uid']]=payload;self.assets.pixel_sizes[asset['uid']]=asset['dimensions'][0]*asset['dimensions'][1];self.action(lambda:self.controller.mutate(change))
        self.task(work,done,'قراءة الصورة وإنشاء معاينة محدودة')
    def annotate(self):
        s,b=self.locate()
        if not b or b['kind']!='image' or not b.get('asset_uid'):return
        from image_editor import ImageEditorDialog
        import numpy as np
        from PIL import Image
        payload=self.assets.payloads[b['asset_uid']]
        origin=self.controller.document.report_uid
        with Image.open(io.BytesIO(payload)) as im:rgb=np.asarray(im.convert('RGB')).copy()
        original_rgb=None
        if b.get('original_asset_uid'):
            with Image.open(io.BytesIO(self.assets.payloads[b['original_asset_uid']])) as im:original_rgb=np.asarray(im.convert('RGB')).copy()
        dialog=ImageEditorDialog(None,{'annotations':b.get('annotations',[]),'transforms':b.get('transforms',[]),'original_available':b.get('original_available',True),**b.get('layer_adjustments',{})},self,report_mode=True,image_rgb=rgb,original_rgb=original_rgb)
        if dialog.exec()==QDialog.Accepted and origin==self.controller.document.report_uid:
            result=dialog.result
            # Working crop/rotation becomes a report-owned image; original remains separately available.
            buf=io.BytesIO();Image.fromarray(result['working_rgb']).save(buf,format='PNG');asset=self.assets.add_png(buf.getvalue(),{'dimensions':[result['working_rgb'].shape[1],result['working_rgb'].shape[0]],'source_ref':b.get('source_ref')})
            def change(d):
                d['assets'][asset['uid']]=asset;item=next(x for sec in d['sections'] for x in sec['blocks'] if x['uid']==b['uid']);item.setdefault('original_asset_uid',item['asset_uid']);item.update(asset_uid=asset['uid'],annotations=result['annotations'],transforms=result['transforms'],composite_asset_uid=None,layer_adjustments={},variant='annotated')
            self.action(lambda:self.controller.mutate(change))
    def zoom_changed(self,value):self.preview.zoom=value/100;self.preview.resizeEvent(None);self.preview.viewport().update()
    def build_preview(self):
        self.generation+=1;generation=self.generation;doc=self.controller.document;store=AssetStore();store.payloads=dict(self.assets.payloads)
        def done(layout):
            if generation==self.generation:self.preview.report_uid=doc.report_uid;self.preview.set_layout(layout);self.status.setText(f'المعاينة: {layout.page_count} صفحة — مراجعة {doc.revision}')
        if len(self.jobs)>=2:self.debounce.start();return
        def progress(image):
            if generation==self.generation:self.preview.report_uid=doc.report_uid;self.preview.set_partial(image);self.status.setText('معاينة أولية؛ يستكمل تخطيط بقية الصفحات')
        self.task(lambda emit:ReportLayout(doc,store,progress=emit).freeze(),done,'تجهيز المعاينة',progress=progress)
    def may_discard(self):
        if not self.controller.dirty and not self.form_dirty:return True
        answer=QMessageBox.question(self,'تعديلات غير محفوظة','احفظ مشروع التقرير قبل الإغلاق؟ التصدير لا يحفظ المشروع.',QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
        if answer==QMessageBox.Save:self.save();return False
        return answer==QMessageBox.Discard
    def open_file(self):
        if not self.may_discard():return
        path,_=QFileDialog.getOpenFileName(self,'فتح مشروع تقرير','','ChangeVision Report (*.cvreport)')
        if path:
            self.load_generation+=1;token=self.load_generation
            def done(result):
                if token==self.load_generation:self.path=Path(path);self.loaded(result,token=token)
            self.task(lambda:load_project(path),done,'التحقق من مشروع التقرير')
    def save(self,as_new=False):
        if self.form_dirty:
            try:self.commit_form()
            except Exception as exc:QMessageBox.warning(self,'تعذر حفظ التعديلات',str(exc));return
        path=str(self.path or '')
        if as_new or not path:
            if not path:
                try:
                    folder=get_storage_path('reports');validate_storage_path(folder,create=True)
                    path=str(folder/'ChangeVision_Report.cvreport')
                except (OSError,ValueError) as exc:
                    QMessageBox.warning(self,'تعذر حفظ التقرير',str(exc));return
            path,_=QFileDialog.getSaveFileName(self,'حفظ مشروع التقرير',path,'ChangeVision Report (*.cvreport)',options=QFileDialog.DontConfirmOverwrite)
            if not path:return
            if not path.lower().endswith('.cvreport'):path+='.cvreport'
        if Path(path).exists() and QMessageBox.question(self,'تأكيد الاستبدال','استبدال مشروع التقرير الموجود؟')!=QMessageBox.Yes:return
        doc=self.controller.document;store=AssetStore();store.payloads=dict(self.assets.payloads)
        def done(_):
            if self.controller.document.report_uid!=doc.report_uid:return
            self.path=Path(path);self.controller.mark_saved_document(doc);self.refresh();self.status.setText('حفظ مشروع التقرير بنجاح')
        self.task(lambda:save_project(path,doc,store,overwrite=True),done,'حفظ صريح والتحقق من سلامة الملف')
    def export(self,kind):
        if self.form_dirty:
            try:self.commit_form()
            except Exception as exc:QMessageBox.warning(self,'تعذر التصدير',str(exc));return
        try:
            folder=get_storage_path('word' if kind=='docx' else 'pdf');validate_storage_path(folder,create=True)
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,'تعذر تصدير التقرير',str(exc));return
        path,_=QFileDialog.getSaveFileName(self,'تصدير التقرير',str(folder/('ChangeVision_Report.'+kind)),kind.upper()+' (*.'+kind+')',options=QFileDialog.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.'+kind):path+='.'+kind
        if Path(path).exists() and QMessageBox.question(self,'تأكيد الاستبدال','استبدال الملف؟')!=QMessageBox.Yes:return
        doc=self.controller.document;store=AssetStore();store.payloads=dict(self.assets.payloads)
        if kind=='docx':from report_docx import export_docx as exporter
        else:from report_layout import export_pdf as exporter
        self.task(lambda:exporter(path,doc,store,overwrite=True),lambda _:self.status.setText('تم التصدير؛ احفظ مشروع التقرير للاحتفاظ بالتحرير'),'تصدير نسخة ثابتة من التقرير')
    def reject(self):
        if self.jobs or self.tree_pending: self.status.setText('انتظر انتهاء العملية قبل إغلاق المحرر');return
        if self.may_discard():super().reject()
    def closeEvent(self,event):
        if self.jobs or self.tree_pending or not self.may_discard():event.ignore()
        else:event.accept()
