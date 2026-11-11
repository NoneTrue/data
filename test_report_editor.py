import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from PIL import Image
from PySide6.QtWidgets import QApplication,QInputDialog,QFileDialog,QMessageBox
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import QPointF
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from report_document import block,ReportController
from report_adapter import from_snapshot
from report_editor import ReportEditorDialog
from report_layout import ReportLayout,export_pdf,MARGIN
from report_docx import export_docx
from report_project import load_project

APP=QApplication.instance() or QApplication([])
for name in ['arial.ttf','arialbd.ttf']:
    QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name))

def wait(dialog):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        APP.processEvents();time.sleep(.005)
        if not dialog.jobs and not dialog.debounce.isActive():return
    raise AssertionError('Editor jobs did not finish')

class ReportEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        p=self.root/'source.png';Image.new('RGB',(80,40),'orange').save(p)
        self.snapshot={'regions':[{'id':15,'target_uid':'stable-15','target_name':'هدف عربي','report_included':True,'report_images':[{'path':str(p),'source':'after'}]}],'change_summary':{'changed_pixels':0}}
        self.original=copy.deepcopy(self.snapshot);self.doc,self.assets=from_snapshot(self.snapshot)
        self.dialog=ReportEditorDialog();self.dialog.loaded((self.doc,self.assets));wait(self.dialog)
        self.addCleanup(self.cleanup_dialog)
    def cleanup_dialog(self):
        wait(self.dialog);self.dialog.controller.mark_saved()
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Discard):self.dialog.reject()
        self.dialog.deleteLater();APP.processEvents()
    def test_sections_hide_restore_delete_and_undo(self):
        key=self.doc.to_dict()['sections'][0]['uid'];self.dialog.select_uid(key);self.dialog.toggle()
        self.assertFalse(self.dialog.controller.document.to_dict()['sections'][0]['visible'])
        self.dialog.restore();self.assertTrue(self.dialog.controller.document.to_dict()['sections'][0]['visible'])
        before=len(self.dialog.controller.document.to_dict()['sections']);self.dialog.select_uid(key);self.dialog.delete()
        self.assertEqual(len(self.dialog.controller.document.to_dict()['sections']),before-1)
        self.dialog.undo();self.assertEqual(len(self.dialog.controller.document.to_dict()['sections']),before)
    def test_text_field_style_and_move(self):
        c=self.dialog.controller;sid=c.add_section('قسم مخصص');b=block('paragraph',text='قبل',locked=False);c.add_block(sid,b);self.dialog.refresh();self.dialog.select_uid(b['uid'])
        self.dialog.content.setPlainText('نص عربي بعد التعديل');self.dialog.size.setValue(18);self.dialog.apply()
        _,item=self.dialog.locate();self.assertEqual(item['text'],'نص عربي بعد التعديل');self.assertEqual(item['style']['size'],18)
        self.assertEqual(self.snapshot,self.original)
        dest=self.doc.to_dict()['sections'][0]['uid'];self.dialog.section_dest.setCurrentIndex(self.dialog.section_dest.findData(dest));self.dialog.move_to_section()
        self.assertEqual(self.dialog.locate()[0]['uid'],dest)
    def test_table_original_columns_protected_custom_column_editable(self):
        table=next(b for s in self.doc.to_dict()['sections'] for b in s['blocks'] if b['kind']=='table');self.dialog.select_uid(table['uid'])
        with patch.object(QInputDialog,'getText',return_value=('مخصص',True)):self.dialog.add_column()
        self.dialog.table.item(0,0).setText('tamper') # UI disabled editing; model boundary also validates.
        self.dialog.table.setItem(0,self.dialog.table.columnCount()-1,__import__('PySide6.QtWidgets',fromlist=['QTableWidgetItem']).QTableWidgetItem('قيمة يدوية'))
        self.dialog.apply_table();_,b=self.dialog.locate();self.assertEqual(b['rows'][0][0],15);self.assertEqual(b['rows'][0][-1],'قيمة يدوية')
        with self.assertRaises(ValueError):self.dialog.controller.edit_table(b['uid'],b['columns'][1:],[r[1:] for r in b['rows']])
    def test_explicit_save_and_open_file(self):
        dest=self.root/'saved.cvreport'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(dest),'')):self.dialog.save()
        wait(self.dialog);self.assertTrue(dest.is_file());self.assertFalse(self.dialog.controller.dirty)
        reopened,assets=load_project(dest);self.assertEqual(reopened.to_dict(),self.dialog.controller.document.to_dict())
        self.assertEqual(list(self.root.glob('*recovery*')),[])
    def test_layout_has_stable_hit_map_and_exports_real_tables(self):
        layout=ReportLayout(self.doc,self.assets);self.assertGreater(len(layout.anchors),0)
        key,rect=next(iter(layout.anchors.items()));page=int(rect.top()/layout.doc.pageSize().height())
        hit=layout.hit(page,QPointF(MARGIN+10,MARGIN+rect.top()-page*layout.doc.pageSize().height()+1))
        self.assertIn(hit,layout.items)
        pdf=self.root/'out.pdf';word=self.root/'out.docx';export_pdf(pdf,self.doc,self.assets);export_docx(word,self.doc,self.assets)
        self.assertTrue(pdf.read_bytes().startswith(b'%PDF-'))
        with zipfile.ZipFile(word) as z:
            from xml.etree import ElementTree as E
            tree=E.fromstring(z.read('word/document.xml'));ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            self.assertGreaterEqual(len(tree.findall('.//w:tbl',ns)),1);self.assertTrue(any(n.startswith('word/media/') for n in z.namelist()))
    def test_unsaved_prompt_cancel_and_discard(self):
        self.dialog.controller.add_section('تغيير')
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Cancel):self.assertFalse(self.dialog.may_discard())
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Discard):self.assertTrue(self.dialog.may_discard())
    def test_pending_edit_commits_on_selection_and_save(self):
        c=self.dialog.controller;sid=c.add_section('مخصص');item=block('paragraph',text='قبل',locked=False);c.add_block(sid,item);self.dialog.refresh();self.dialog.select_uid(item['uid'])
        self.dialog.content.setPlainText('تعديل لم يضغط تطبيق');self.assertTrue(self.dialog.form_dirty)
        self.dialog.select_uid(self.doc.to_dict()['sections'][0]['uid']);self.assertFalse(self.dialog.form_dirty)
        found=next(b for s in c.document.to_dict()['sections'] for b in s['blocks'] if b['uid']==item['uid']);self.assertEqual(found['text'],'تعديل لم يضغط تطبيق')
    def test_new_window_uses_existing_entry_without_main_startup(self):
        from report_export import ReportBuilderDialog
        self.assertIs(ReportBuilderDialog,ReportEditorDialog)
        import ast
        tree=ast.parse(Path('main.py').read_text(encoding='utf-8-sig'));method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='open_report_builder')
        self.assertTrue(any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='ReportBuilderDialog' for n in ast.walk(method)))
    def test_stale_load_and_save_cannot_replace_another_report(self):
        old_uid=self.dialog.controller.document.report_uid
        another,store=from_snapshot({'regions':[],'report_metadata':{'operation_name':'تقرير آخر'}})
        self.dialog.load_generation=2;self.dialog.loaded((another,store),token=1)
        self.assertEqual(self.dialog.controller.document.report_uid,old_uid)
        pending=[];dest=self.root/'old.cvreport'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(dest),'')),patch.object(self.dialog,'task',side_effect=lambda fn,callback,message:pending.append(callback)):
            self.dialog.save()
        self.dialog.loaded((another,store),token=2);pending[0](None)
        self.assertEqual(self.dialog.controller.document.report_uid,another.report_uid);self.assertIsNone(self.dialog.path)
        self.assertFalse(self.dialog.controller.dirty)
    def test_target_exclusion_is_report_local_and_filters_all_linked_content(self):
        from report_document import item_included,rows_for_report
        before=self.dialog.controller.document.analysis_snapshot
        self.dialog.target.setCurrentIndex(self.dialog.target.findData('stable-15'));self.dialog.toggle_target()
        data=self.dialog.controller.document.to_dict();self.assertFalse(data['target_selection']['stable-15']);self.assertEqual(self.snapshot,self.original);self.assertEqual(before,data['analysis_snapshot'])
        table=next(b for s in data['sections'] for b in s['blocks'] if b['kind']=='table');self.assertEqual(rows_for_report(data,table),[])
        self.assertFalse(any(item_included(data,b) for s in data['sections'] for b in s['blocks'] if b.get('target_uid')=='stable-15'))
        self.dialog.undo();self.assertTrue(self.dialog.controller.document.to_dict()['target_selection']['stable-15'])
    def test_actual_preview_click_selects_editable_block(self):
        from report_layout import MARGIN,BODY_H,HEIGHT
        c=self.dialog.controller;sid=c.add_section('قسم نقر');b=block('paragraph',text='فقرة يمكن تحديدها من المعاينة',locked=False);c.add_block(sid,b);self.dialog.refresh();wait(self.dialog)
        preview=self.dialog.preview;layout=preview.layout;self.assertIn(b['uid'],layout.anchors)
        rect=layout.anchors[b['uid']];page=int(rect.top()/BODY_H);step=HEIGHT*preview.zoom+20;preview.verticalScrollBar().setValue(int(page*step))
        pos=QPointF(10+(MARGIN+10)*preview.zoom,10+(MARGIN+rect.top()-page*BODY_H+2)*preview.zoom+page*step-preview.verticalScrollBar().value()).toPoint()
        QTest.mouseClick(preview.viewport(),Qt.LeftButton,Qt.NoModifier,pos);APP.processEvents()
        self.assertEqual(self.dialog.selected_uid,b['uid']);self.assertEqual(self.dialog.content.toPlainText(),b['text'])
    def test_reset_design_preserves_content_and_is_undoable(self):
        c=self.dialog.controller;s=c.add_section('مخصص');b=block('paragraph',text='نص محفوظ',style={'size':20,'color':'#ff0000'},locked=False);c.add_block(s,b)
        before=c.document.to_dict();c.reset_design();after=c.document.to_dict();item=next(b for sec in after['sections'] for b in sec['blocks'] if b['uid']==before['sections'][-1]['blocks'][0]['uid'])
        self.assertEqual(item['text'],'نص محفوظ');self.assertEqual(item['style'],{});c.undo();self.assertEqual(c.document.to_dict(),before)
    def test_replacing_image_changes_original_variant_without_target_swap(self):
        from report_layout import image_payload
        image=next(b for s in self.doc.to_dict()['sections'] for b in s['blocks'] if b['kind']=='image');self.dialog.select_uid(image['uid']);before=image_payload(image,self.assets)
        new=self.root/'replacement.png';Image.new('RGB',(80,40),'blue').save(new)
        with patch.object(QFileDialog,'getOpenFileName',return_value=(str(new),'')):self.dialog.import_image(True)
        wait(self.dialog);after=next(b for s in self.dialog.controller.document.to_dict()['sections'] for b in s['blocks'] if b['uid']==image['uid'])
        self.assertEqual(after['placement_uid'],image['placement_uid']);self.assertEqual(after['target_uid'],'stable-15');self.assertNotEqual(image_payload(after,self.dialog.assets),before)
        with Image.open(io.BytesIO(image_payload(after,self.dialog.assets))) as im:self.assertEqual(im.getpixel((10,10)),(0,0,255))
        self.dialog.undo();restored=next(b for s in self.dialog.controller.document.to_dict()['sections'] for b in s['blocks'] if b['uid']==image['uid']);self.assertEqual(image_payload(restored,self.dialog.assets),before)
    def test_legacy_html_id_and_metadata_fallback_do_not_lose_values(self):
        from report_export import report_html
        from lxml import html
        markup=report_html({'image1':{'path':'before_image.tif'},'report_metadata':{'operation_name':'عملية مرجعية'},'regions':[{'id':15,'report_included':True}],'region_columns':[{'key':'target_id','title':'ID'},{'key':'operation_name','title':'العملية'}]},['overview','targets'])
        tree=html.fromstring(markup);target_table=tree.xpath('//table')[1];self.assertEqual([''.join(c.itertext()) for c in target_table.xpath('.//tr')[1].xpath('./td')],['15','عملية مرجعية']);self.assertIn('before_image.tif',markup)

if __name__=='__main__':unittest.main()
