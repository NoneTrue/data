import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy,json,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
from lxml import etree as E
from PySide6.QtCore import QSettings,Qt
from PySide6.QtWidgets import QApplication,QLineEdit,QMessageBox
from PySide6.QtTest import QTest
from report_manager import write_xlsx
from region_excel import RegionWorkbook,RegionAutosave,digest,NS

APP=QApplication.instance() or QApplication([])
COLS=[{'key':'target_id','title':'ID'},{'key':'target_name','title':'Name'},
      {'key':'longitude','title':'Longitude'},{'key':'notes','title':'Notes'}]

def rows(regions):return [['ID','Name','Longitude','Notes']]+[[r['id'],r['target_name'],r['longitude'],r['notes']] for r in regions]
def read(path):
    with zipfile.ZipFile(path) as z:return E.fromstring(z.read('xl/worksheets/sheet1.xml'))
def cell(path,ref):return ''.join(read(path).find('.//{%s}c[@r="%s"]'%(NS,ref)).itertext())
def wait(manager):
    APP.processEvents()
    end=time.monotonic()+8
    while manager.dirty and time.monotonic()<end:APP.processEvents();time.sleep(.01)
    if manager.dirty:raise AssertionError('Pending Excel edits remain')

class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'بيانات تجربة';self.root.mkdir()
        self.path=self.root/'Analysis_Change_Regions.xlsx'
        self.regions=[dict(id=183,target_uid='T183',target_name='',longitude=49.123,latitude=11.2,notes=''),
                      dict(id=184,target_uid='T184',target_name='Other',longitude=49.2,latitude=11.3,notes='unchanged')]
        write_xlsx(self.path,[('Regions',rows(self.regions)),('Other',[['Keep'],['=SUM(1,2)']])])
        self.store=RegionWorkbook.create(self.path,self.regions,COLS,['before.tif','after.tif'])
    def test_patches_only_committed_metadata_and_preserves_other_zip_parts(self):
        with zipfile.ZipFile(self.path) as z:before={n:z.read(n) for n in z.namelist()}
        self.regions[0]['target_name']='ميناء 183';self.regions[0]['notes']='new notes'
        self.store.update(self.regions,COLS,{})
        self.assertEqual(cell(self.path,'B2'),'ميناء 183');self.assertEqual(cell(self.path,'A2'),'183')
        self.assertEqual(cell(self.path,'C2'),'49.123');self.assertEqual(cell(self.path,'B3'),'Other')
        with zipfile.ZipFile(self.path) as z:
            for n,payload in before.items():
                if n!='xl/worksheets/sheet1.xml':self.assertEqual(payload,z.read(n))
        original=E.fromstring(before['xl/worksheets/sheet1.xml'])
        self.assertEqual(original.find('.//{%s}c[@r="B2"]'%NS).get('s'),read(self.path).find('.//{%s}c[@r="B2"]'%NS).get('s'))
    def test_external_change_is_never_overwritten(self):
        with zipfile.ZipFile(self.path,'a') as z:z.writestr('external.txt','external edit')
        before=digest(self.path)
        with self.assertRaisesRegex(ValueError,'تعارض'):self.store.update(self.regions,COLS,{})
        self.assertEqual(digest(self.path),before)
    def test_native_windows_lock_and_retry(self):
        if os.name!='nt':self.skipTest('Windows native sharing lock')
        import ctypes
        from ctypes import wintypes
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];api.CreateFileW.restype=wintypes.HANDLE
        h=api.CreateFileW(str(self.path),0x80000000,0,None,3,0,None)
        self.assertNotEqual(h,ctypes.c_void_p(-1).value)
        try:
            with self.assertRaises(PermissionError):self.store.update(self.regions,COLS,{})
        finally:api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle(h)
        self.regions[0]['target_name']='retry';self.store.update(self.regions,COLS,{})
        self.assertEqual(cell(self.path,'B2'),'retry')
    def test_real_formula_preserved_and_formula_cell_edit_rejected(self):
        temporary=self.root/'formula.xlsx'
        with zipfile.ZipFile(self.path) as source,zipfile.ZipFile(temporary,'w') as output:
            for entry in source.infolist():
                payload=source.read(entry.filename)
                if entry.filename=='xl/worksheets/sheet1.xml':
                    sheet=E.fromstring(payload);node=sheet.find('.//{%s}c[@r="D3"]'%NS)
                    for child in list(node):node.remove(child)
                    node.set('t','n');E.SubElement(node,'{%s}f'%NS).text='SUM(1,2)';E.SubElement(node,'{%s}v'%NS).text='3'
                    payload=E.tostring(sheet)
                output.writestr(entry,payload)
        os.replace(temporary,self.path)
        self.store=RegionWorkbook.create(self.path,self.regions,COLS,['before.tif','after.tif'])
        self.regions[0]['target_name']='Safe';self.store.update(self.regions,COLS,{})
        self.assertEqual(read(self.path).find('.//{%s}c[@r="D3"]/{%s}f'%(NS,NS)).text,'SUM(1,2)')
        self.regions[1]['notes']='replace formula'
        with self.assertRaises(ValueError):self.store.update(self.regions,COLS,{})
    def test_windows_readonly_file_is_preserved(self):
        if os.name!='nt':self.skipTest('Windows read-only attribute')
        import stat
        before=digest(self.path);self.path.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(PermissionError):self.store.update(self.regions,COLS,{})
            self.assertEqual(digest(self.path),before)
        finally:self.path.chmod(stat.S_IWRITE|stat.S_IREAD)
    def test_atomic_replace_failure_leaves_workbook_intact(self):
        before=digest(self.path);replace=os.replace
        def fail(source,target):
            if Path(target)==self.path:raise PermissionError('injected failed replace')
            return replace(source,target)
        self.regions[0]['target_name']='pending'
        with patch('region_excel.os.replace',side_effect=fail):
            with self.assertRaises(PermissionError):self.store.update(self.regions,COLS,{})
        self.assertEqual(digest(self.path),before);self.assertEqual(list(self.root.glob('.cvexcel_*.xlsx')),[])
    def test_pending_manifest_recovers_after_workbook_commit(self):
        replace=os.replace
        def fail(source,target):
            if Path(target)==self.store.manifest and str(source).endswith('.pending'):raise OSError('interrupted manifest commit')
            return replace(source,target)
        self.regions[0]['target_name']='persisted'
        with patch('region_excel.os.replace',side_effect=fail):
            with self.assertRaises(OSError):self.store.update(self.regions,COLS,{})
        # Discover uses the directory naming convention of real analysis sessions.
        folder=self.root/'Analysis_Tables_test';folder.mkdir()
        for file in list(self.root.glob('Analysis_Change*')):file.rename(folder/file.name)
        store=RegionWorkbook.discover(self.root,self.regions,['before.tif','after.tif'])
        self.assertIsNotNone(store);self.assertEqual(cell(store.path,'B2'),'persisted')
    def test_restore_reordered_targets_and_session_isolation(self):
        self.regions[0]['target_name']='Saved';self.store.update(self.regions,COLS,{})
        stale=list(reversed(copy.deepcopy(self.regions)));stale[1]['target_name']='stale'
        self.store.restore(stale);self.assertEqual(stale[1]['target_name'],'Saved')
        wrong=copy.deepcopy(self.regions);wrong[0]['id']=999
        with self.assertRaises(ValueError):self.store.update(wrong,COLS,{})
        wrong=copy.deepcopy(self.regions);wrong[0]['longitude']=10
        with self.assertRaises(ValueError):self.store.update(wrong,COLS,{})
    def test_schema_edits_and_manual_rows_keep_original_rows(self):
        cols=copy.deepcopy(COLS);cols[1]['title']='Renamed';cols.append({'key':'custom_new','title':'Custom'})
        self.regions[0]['excel_values']={'custom_new':'Added'}
        self.regions.append(dict(id=185,target_uid='M185',target_name='Manual',manual_target=True,notes=''))
        self.store.update(self.regions,cols,{})
        self.assertEqual(cell(self.path,'B1'),'Renamed');self.assertEqual(cell(self.path,'E2'),'Added');self.assertEqual(cell(self.path,'A4'),'185')
        self.regions.pop();self.store.update(self.regions,COLS,{})
        self.assertEqual(cell(self.path,'A4'),'');self.assertEqual(cell(self.path,'E2'),'');self.assertEqual(cell(self.path,'A2'),'183')
    def test_debounce_manual_flush_and_last_revision(self):
        manager=RegionAutosave(APP);manager.bind(self.store);errors=[];manager.failed.connect(errors.append)
        with patch.object(self.store,'update',wraps=self.store.update) as update:
            for name in ['one','two','last']:
                self.regions[0]['target_name']=name;manager.queue(self.regions,COLS,{})
            self.assertEqual(update.call_count,0);manager.flush();wait(manager)
            self.assertEqual(update.call_count,1);self.assertFalse(errors)
        self.assertEqual(cell(self.path,'B2'),'last')
    def test_missing_drive_and_write_denial_keep_pending_ui_value(self):
        manager=RegionAutosave(APP);manager.bind(self.store);errors=[];manager.failed.connect(errors.append)
        self.regions[0]['target_name']='keep UI'
        with patch('region_excel.validate_storage_path',side_effect=PermissionError('denied')):
            manager.queue(self.regions,COLS,{});manager.flush()
            deadline=time.monotonic()+4
            while manager.worker is not None and time.monotonic()<deadline:APP.processEvents();time.sleep(.01)
        self.assertTrue(manager.dirty);self.assertTrue(errors);self.assertEqual(self.regions[0]['target_name'],'keep UI')
        manager.flush();wait(manager)
        self.store.path=self.root/'missing disk'/'Analysis_Change_Regions.xlsx'
        with self.assertRaises(FileNotFoundError):self.store.update(self.regions,COLS,{})

    def test_edits_during_save_are_serialized_and_event_loop_keeps_running(self):
        manager=RegionAutosave(APP);manager.bind(self.store)
        update=self.store.update;events=[]
        from PySide6.QtCore import QTimer
        timer=QTimer();timer.timeout.connect(lambda:events.append(1));timer.start(10)
        def slow(*args):time.sleep(.12);return update(*args)
        with patch.object(self.store,'update',side_effect=slow) as calls:
            self.regions[0]['target_name']='first';manager.queue(self.regions,COLS,{});manager.flush()
            QTest.qWait(30)
            self.regions[0]['target_name']='last during save';manager.queue(self.regions,COLS,{});manager.flush()
            wait(manager);self.assertEqual(calls.call_count,2)
        timer.stop();self.assertGreater(len(events),5)
        self.assertEqual(cell(self.path,'B2'),'last during save')

class MainTableTests(unittest.TestCase):
    def test_actual_main_table_edit_autosave_empty_file_cause_reload_and_close(self):
        import storage_manager
        from main import MainWindow
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=QSettings(str(root/'settings.ini'),QSettings.IniFormat)
            for key in storage_manager.STORAGE_LABELS:
                folder=root/key;folder.mkdir();settings.setValue('storage/'+key,str(folder))
            with patch.object(storage_manager,'_settings',return_value=settings),patch.object(MainWindow,'_update_device'),patch.object(MainWindow,'_cleanup_stale_temp_files'):
                window=MainWindow();window.show();APP.processEvents()
                window.regions=[dict(id=183,target_uid='T183',target_name='',report_included=False,area_pixels=4,area_map_units2=1,longitude=49.1,latitude=11.2,mean_intensity=.2,max_intensity=.4,notes='')]
                window._fill_regions()
                self.assertEqual(len(window._region_excel_rows()),1)
                self.assertEqual(len(window._region_excel_rows(include_all=True)),2)
                path,_=window._save_analysis_tables_excel(None);self.assertTrue(Path(path).is_file())
                index=11+next(i for i,c in enumerate(window._region_edit_columns) if c['key']=='target_name')
                item=window.table.item(0,index);window.table.scrollToItem(item);APP.processEvents()
                position=window.table.visualItemRect(item).center()
                QTest.mouseClick(window.table.viewport(),Qt.LeftButton,Qt.NoModifier,position)
                QTest.mouseDClick(window.table.viewport(),Qt.LeftButton,Qt.NoModifier,position);APP.processEvents()
                editors=[e for e in window.table.findChildren(QLineEdit) if e.isVisible()];self.assertEqual(len(editors),1)
                QTest.keyClicks(editors[0],'Autosaved183');QTest.keyClick(editors[0],Qt.Key_Return)
                wait(window.region_autosave)
                targetcol=next(c['slot'] for c in window.region_autosave.store.state['columns'] if c['key']=='target_name')
                from region_excel import letter
                self.assertEqual(cell(path,letter(targetcol)+'2'),'Autosaved183')
                self.assertFalse(window.table.item(0,1).flags()&Qt.ItemIsEditable)
                window.regions[0]['target_name']='old CVP value';window.region_autosave.bind(None);window._restore_region_excel()
                self.assertEqual(window.regions[0]['target_name'],'Autosaved183')
                window._fill_regions();window.table.item(0,index).setText('FinalOnClose');window.close()
                self.assertTrue(window.isVisible());wait(window.region_autosave);APP.processEvents()
                self.assertEqual(cell(path,letter(targetcol)+'2'),'FinalOnClose');self.assertFalse(window.isVisible())
                window.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main()
