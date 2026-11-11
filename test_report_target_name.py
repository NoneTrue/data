"""Exercise name editing through the actual report table delegate and exporters."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QInputMethodEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QLineEdit,QFileDialog,QMessageBox
from PySide6.QtPdf import QPdfDocument
from report_adapter import from_snapshot
from report_editor import ReportEditorDialog
from report_project import load_project
from report_layout import export_pdf,ReportLayout
from report_docx import export_docx

APP=QApplication.instance() or QApplication([])

def wait(dialog):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        APP.processEvents();time.sleep(.005)
        if not dialog.jobs and not dialog.debounce.isActive():return
    raise AssertionError('Report editor did not finish')

class TargetNameTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        image=self.root/'before.png';Image.new('RGB',(40,30),'orange').save(image)
        self.snapshot={'regions':[
            {'id':183,'target_uid':'T183','target_name':'','longitude':49.123,'report_included':True,
             'report_images':[{'path':str(image),'source':'before'}]},
            {'id':184,'target_uid':'T184','target_name':'Existing 184','report_included':True}]}
        self.original=copy.deepcopy(self.snapshot)
        doc,assets=from_snapshot(self.snapshot)
        self.before=doc.to_dict();self.asset_bytes=copy.deepcopy(assets.payloads)
        self.dialog=ReportEditorDialog();self.dialog.loaded((doc,assets));self.dialog.show();wait(self.dialog)
        self.addCleanup(self.close_dialog)
        self.table=next(b for s in doc.sections_for_view() for b in s['blocks'] if b.get('source')=='included_targets')
        self.dialog.select_uid(self.table['uid']);APP.processEvents()
        self.name_col=next(i for i,c in enumerate(self.table['columns']) if c['key']=='target_name')

    def close_dialog(self):
        wait(self.dialog)
        with patch.object(QMessageBox,'question',return_value=QMessageBox.Discard):self.dialog.close()
        self.dialog.deleteLater();APP.processEvents()

    def type_name(self,name):
        table=self.dialog.table;item=table.item(0,self.name_col)
        self.assertTrue(item.flags() & Qt.ItemIsEditable)
        position=table.visualItemRect(item).center()
        QTest.mouseClick(table.viewport(),Qt.LeftButton,Qt.NoModifier,position)
        QTest.mouseDClick(table.viewport(),Qt.LeftButton,Qt.NoModifier,position);APP.processEvents()
        editors=[e for e in table.findChildren(QLineEdit) if e.isVisible()]
        self.assertEqual(len(editors),1,'Double click must open a real cell editor')
        editor=editors[0];QTest.keyClick(editor,Qt.Key_A,Qt.ControlModifier)
        event=QInputMethodEvent();event.setCommitString(name);APP.sendEvent(editor,event)
        QTest.keyClick(editor,Qt.Key_Return);APP.processEvents()
        self.assertEqual(item.text(),name)

    def test_id183_typing_preview_save_reopen_word_pdf_and_no_source_mutation(self):
        name='Bosaso Landmark 183'
        self.type_name(name)
        # Save must commit pending table edits even without clicking Apply.
        path=self.root/'edited.cvreport'
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(path),'')):self.dialog.save()
        wait(self.dialog)
        data=self.dialog.controller.document.to_dict()
        self.assertFalse(self.dialog.controller.dirty)
        self.assertEqual(self.dialog.preview.layout.items[self.table['uid']]['rows'][0][self.name_col],name)
        self.assertIn(name,ReportLayout(self.dialog.controller.document,self.dialog.assets).doc.toPlainText())
        reopened,assets=load_project(path)
        table=next(b for s in reopened.sections_for_view() for b in s['blocks'] if b.get('source')=='included_targets')
        self.assertEqual(table['rows'][0][self.name_col],name)
        self.dialog.loaded((reopened,assets));wait(self.dialog);self.dialog.select_uid(table['uid'])
        self.assertEqual(self.dialog.table.item(0,self.name_col).text(),name)
        word=self.root/'edited.docx';pdf=self.root/'edited.pdf'
        export_docx(word,reopened,assets);export_pdf(pdf,reopened,assets)
        with zipfile.ZipFile(word) as archive:
            self.assertIn(name,archive.read('word/document.xml').decode('utf-8'))
            self.assertTrue(any(n.startswith('word/media/') for n in archive.namelist()))
        reader=QPdfDocument();self.assertEqual(reader.load(str(pdf)),QPdfDocument.Error.None_)
        text='\n'.join(reader.getAllText(i).text() for i in range(reader.pageCount()))
        # Qt PDF selection returns RTL table words in visual order.
        name_line=next(line for line in text.splitlines() if 'Bosaso' in line)
        for word_part in name.split():self.assertIn(word_part,name_line)
        evidence=os.environ.get('CV_TARGET_NAME_EVIDENCE')
        if evidence:
            from PySide6.QtCore import QSize
            out=Path(evidence);out.mkdir(parents=True,exist_ok=True)
            reader.render(1,QSize(1190,1684)).save(str(out/'pdf_target_table.png'))
        reader.close()
        self.assertEqual(self.snapshot,self.original)

        self.assertEqual(data['analysis_snapshot'],self.before['analysis_snapshot'])
        self.assertEqual(data['targets'],self.before['targets'])
        self.assertEqual(data['assets'],self.before['assets'])
        self.assertEqual(assets.payloads,self.asset_bytes)
        for old,new in zip(self.before['sections'],data['sections']):
            if old['kind']=='targets':
                expected=copy.deepcopy(old);expected['blocks'][0]['rows'][0][self.name_col]=name
                # Existing form commits also capture its current formatting
                # and the original row count, independently of this fix.
                expected['blocks'][0]['style']=new['blocks'][0]['style']
                expected['blocks'][0]['source_row_count']=2
                self.assertEqual(new,expected)
            else:self.assertEqual(new,old)
        evidence=os.environ.get('CV_TARGET_NAME_EVIDENCE')
        if evidence:
            import shutil
            out=Path(evidence);out.mkdir(parents=True,exist_ok=True)
            for file in [path,word,pdf]:shutil.copyfile(file,out/file.name)
            self.dialog.grab().save(str(out/'editor.png'))

    def test_other_table_target_name_column_stays_locked(self):
        from report_document import block
        controller=self.dialog.controller
        other=block('table',locked=True,columns=[{'key':'target_name','title':'Other','locked':True}],rows=[['protected']])
        controller.add_block(controller.document.sections_for_view()[0]['uid'],other)
        self.dialog.refresh();self.dialog.select_uid(other['uid'])
        self.assertFalse(self.dialog.table.item(0,0).flags() & Qt.ItemIsEditable)
        with self.assertRaises(ValueError):controller.edit_table(other['uid'],other['columns'],[['changed']])

    def test_arabic_name_undo_redo_and_protected_cells(self):
        self.type_name('ميناء بوصاصو 183');self.dialog.apply_table();wait(self.dialog)
        self.assertEqual(self.dialog.preview.layout.items[self.table['uid']]['rows'][0][self.name_col],'ميناء بوصاصو 183')
        self.dialog.undo();wait(self.dialog)
        self.assertEqual(self.dialog.table.item(0,self.name_col).text(),'غير مدخل')
        self.dialog.redo();wait(self.dialog)
        self.assertEqual(self.dialog.table.item(0,self.name_col).text(),'ميناء بوصاصو 183')
        for col in range(self.dialog.table.columnCount()):
            if col!=self.name_col:self.assertFalse(self.dialog.table.item(0,col).flags() & Qt.ItemIsEditable)
        current=self.dialog.locate()[1]
        changed=copy.deepcopy(current['rows']);changed[0][0]=999
        with self.assertRaises(ValueError):self.dialog.controller.edit_table(current['uid'],current['columns'],changed)
        with self.assertRaises(ValueError):self.dialog.controller.edit_table(current['uid'],current['columns'][:1],[[183],[184]])
        self.assertEqual(self.snapshot,self.original)

if __name__=='__main__':unittest.main()
