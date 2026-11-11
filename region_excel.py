"""Session-bound, atomic XLSX metadata updates. No analysis computations."""
from __future__ import annotations
import copy,hashlib,json,os,tempfile,uuid,zipfile
from pathlib import Path
from contextlib import contextmanager
from lxml import etree as E
from PySide6.QtCore import QObject,QThread,QTimer,Signal
from storage_manager import validate_storage_path

NS='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
PROTECTED={'navigate','target_id','id','longitude','latitude','object_id','area_pixels',
           'area_map_units2','mean_intensity','max_intensity','confidence','classification'}

def editable(key):
    return key not in PROTECTED

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def identity(regions,images):
    values={'images':[os.path.normcase(str(Path(p).resolve())) if p else '' for p in images],
            'targets':sorted([(str(r['target_uid']),r.get('id'),r.get('longitude'),r.get('latitude')) for r in regions])}
    return hashlib.sha256(json.dumps(values,sort_keys=True,default=str).encode()).hexdigest()

def letter(number):
    result=''
    while number:number,n=divmod(number-1,26);result=chr(65+n)+result
    return result

def atomic_json(path,data):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

@contextmanager
def writable_file(path):
    """Ask Windows for delete access too, so Excel's open-file lock is honored."""
    if path.with_name('~$'+path.name).exists():raise PermissionError('ملف Excel مفتوح؛ أغلقه ثم أعد الحفظ.')
    if os.name!='nt':yield lambda:None;return
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    api.CreateFileW.restype=wintypes.HANDLE
    handle=api.CreateFileW(str(path),0x80000000|0x10000,1|4,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:raise PermissionError('ملف Excel مقفل أو لا تسمح الصلاحيات باستبداله بأمان.')
    api.CloseHandle.argtypes=[wintypes.HANDLE];api.CloseHandle(handle)
    # Keep write sharing denied while ordinary Python read handles are opened.
    handle=api.CreateFileW(str(path),0x80000000,1|4,None,3,0,None)
    if handle==ctypes.c_void_p(-1).value:raise PermissionError('تعذر حجز ملف Excel للقراءة الآمنة.')
    held=[True]
    def release():
        if held[0]:api.CloseHandle(handle);held[0]=False
    try:yield release
    finally:
        release()

class RegionWorkbook:
    def __init__(self,path,state):
        self.path=Path(path);self.state=state
        self.manifest=self.path.with_name(self.path.name+'.cvsession.json')

    @classmethod
    def create(cls,path,regions,columns,images,metadata=None):
        path=Path(path)
        if not regions:raise ValueError('لا يُربط ملف مناطق بلا أهداف.')
        state={'format':'CVRegionExcel1','session':uuid.uuid4().hex,'identity':identity(regions,images),
               'sha256':digest(path),'images':list(images),'columns':[{**c,'slot':i+1} for i,c in enumerate(columns)],
               'rows':{str(r['target_uid']):{'slot':i+2,'id':r.get('id',r.get('object_id')),'coordinates':[r.get('longitude'),r.get('latitude')],'manual':bool(r.get('manual_target'))} for i,r in enumerate(regions)}}
        with zipfile.ZipFile(path) as archive:
            cells=E.fromstring(archive.read('xl/worksheets/sheet1.xml')).findall('.//{%s}c'%NS)
            state['values']={c.get('r'):''.join(c.itertext()) for c in cells}
            for cell in cells:
                if cell.find('{%s}f'%NS) is None:continue
                ref=cell.get('r');row=int(ref.lstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ'))-2
                column=next((c for i,c in enumerate(columns,1) if ref.rstrip('0123456789')==letter(i)),None)
                if column and 0<=row<len(regions) and editable(column['key']):
                    key=column['key'];target=regions[row]
                    value=target.get('excel_values',{}).get(key,target.get(key,''))
                    if value in ('',None):value=(metadata or {}).get(key,value)
                    state['values'][ref]='' if value is None else str(value)
        state['active_columns']=copy.deepcopy(columns)
        store=cls(path,state);atomic_json(store.manifest,state);return store

    @classmethod
    def discover(cls,root,regions,images):
        if not regions:return None
        signature=identity(regions,images);matches=[]
        for manifest in Path(root).glob('Analysis_Tables_*/*.xlsx.cvsession.json'):
            state=json.loads(manifest.read_text(encoding='utf-8'))
            if state.get('identity')==signature:matches.append(cls(Path(str(manifest)[:-len('.cvsession.json')]),state))
        if len(matches)>1:raise ValueError('أكثر من ملف Excel مرتبط بهذه الأهداف؛ لن يُختار ملف بالتخمين.')
        if not matches:return None
        store=matches[0];current=digest(store.path)
        pending=store.manifest.with_suffix(store.manifest.suffix+'.pending')
        if pending.exists():
            staged=json.loads(pending.read_text(encoding='utf-8'))
            if staged['sha256']==current:os.replace(pending,store.manifest);store.state=staged
        if store.state['sha256']!=current:raise ValueError('تغير ملف Excel خارج البرنامج؛ أوقف الحفظ التلقائي لحماية التعديلات الخارجية.')
        return store

    def restore(self,regions):
        """Read only mapped metadata, never ID or coordinates, on a proven match."""
        with zipfile.ZipFile(self.path) as z:
            sheet=E.fromstring(z.read('xl/worksheets/sheet1.xml'))
            shared=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                shared=[''.join(n.itertext()) for n in E.fromstring(z.read('xl/sharedStrings.xml'))]
        cells={c.get('r'):c for c in sheet.findall('.//{%s}c'%NS)}
        for target in regions:
            row=self.state['rows'].get(str(target['target_uid']))
            if not row or row['id']!=target.get('id',target.get('object_id')):raise ValueError('ارتباط صف Excel غير مطابق للهدف.')
            for column in self.state['columns']:
                if not editable(column['key']):continue
                cell=cells.get(letter(column['slot'])+str(row['slot']))
                if cell is None:continue
                if cell.find('{%s}f'%NS) is not None:continue
                value=''.join(cell.itertext())
                if cell.get('t')=='s':value=shared[int(value)]
                if column['key'].startswith('custom_'):target.setdefault('excel_values',{})[column['key']]=value
                else:target[column['key']]=value

    def update(self,regions,columns,metadata):
        validate_storage_path(self.path.parent,create=False)
        lock=self.path.with_name(self.path.name+'.cvsave.lock')
        reservation=None;temporary=None
        try:
            reservation=lock.open('x')
            with writable_file(self.path) as release:
                if digest(self.path)!=self.state['sha256']:raise ValueError('تعارض: تغير ملف Excel خارجيًا؛ لم يُستبدل الملف.')
                state=copy.deepcopy(self.state)
                with zipfile.ZipFile(self.path) as source:
                    sheet=E.fromstring(source.read('xl/worksheets/sheet1.xml'))
                    data=sheet.find('{%s}sheetData'%NS)
                    cells={c.get('r'):c for c in data.findall('.//{%s}c'%NS)}
                    def put(row,col,value):
                        ref=letter(col)+str(row);cell=cells.get(ref)
                        text='' if value is None else str(value)
                        if state.get('values',{}).get(ref)==text:return
                        if cell is not None and cell.find('{%s}f'%NS) is not None:
                            raise ValueError('خلية التعديل تحتوي صيغة Excel؛ لم تُستبدل الصيغة.')
                        if cell is None:
                            element=next((r for r in data if int(r.get('r'))==row),None)
                            if element is None:element=E.SubElement(data,'{%s}row'%NS,r=str(row))
                            cell=E.SubElement(element,'{%s}c'%NS,r=ref);cells[ref]=cell
                        for child in list(cell):cell.remove(child)
                        cell.set('t','inlineStr');node=E.SubElement(E.SubElement(cell,'{%s}is'%NS),'{%s}t'%NS)
                        node.set('{http://www.w3.org/XML/1998/namespace}space','preserve');node.text=text
                        state.setdefault('values',{})[ref]=text
                    known={c['key']:c for c in state['columns']}
                    # Existing physical slots are stable: no shift of formulas or unrelated data.
                    for column in columns:
                        key=column['key']
                        if key not in known:
                            if not editable(key):continue
                            slot=max([c['slot'] for c in known.values()]+[0])+1
                            if any(ref.startswith(letter(slot)) and ref[len(letter(slot)):].isdigit() for ref in cells):
                                raise ValueError('العمود الجديد يتداخل مع بيانات غير مرتبطة؛ لم يُستبدل الملف.')
                            known[key]={**column,'slot':slot}
                        if editable(key):put(1,known[key]['slot'],column.get('title',key))
                    active={c['key'] for c in columns}
                    for key,column in known.items():
                        if editable(key) and key not in active:
                            put(1,column['slot'],'')
                            for row in state['rows'].values():put(row['slot'],column['slot'],'')
                    for target in regions:
                        uid=str(target['target_uid']);row=state['rows'].get(uid)
                        if row is None:
                            if not target.get('manual_target'):raise ValueError('أضيف هدف تحليلي غير مربوط بهذا الملف؛ احفظ تحليلًا جديدًا قبل تبديل مجموعة الأهداف.')
                            slot=max([v['slot'] for v in state['rows'].values()]+[1])+1
                            if any(ref.endswith(str(slot)) and ref.rstrip('0123456789')+str(slot)==ref for ref in cells):raise ValueError('صف جديد يتداخل مع بيانات غير مرتبطة.')
                            row={'slot':slot,'id':target.get('id'),'manual':True};state['rows'][uid]=row
                            for column in columns:
                                if not editable(column['key']):put(slot,known[column['key']]['slot'],target.get('id') if column['key']=='target_id' else target.get(column['key'],''))
                        if row['id']!=target.get('id',target.get('object_id')):raise ValueError('تغير معرف الهدف؛ أوقف الحفظ لحماية السجل الأصلي.')
                        if 'coordinates' in row and row['coordinates']!=[target.get('longitude'),target.get('latitude')]:raise ValueError('تغيرت إحداثيات الهدف الأصلي؛ لم يُستبدل الملف.')
                        for column in columns:
                            key=column['key']
                            if not editable(key):continue
                            value=target.get('excel_values',{}).get(key,target.get(key,''))
                            if value in ('',None) and key in metadata:value=metadata[key]
                            put(row['slot'],known[key]['slot'],value)
                    state['columns']=list(known.values())
                    state['active_columns']=copy.deepcopy(columns)
                    state['identity']=identity(regions,state['images'])
                    # Removed manual rows can be cleared; analytical rows remain intact.
                    active_uids={str(r['target_uid']) for r in regions}
                    for uid,row in list(state['rows'].items()):
                        if uid not in active_uids and row.get('manual'):
                            for column in known.values():put(row['slot'],column['slot'],'')
                            row['removed']=True
                    fd,temporary=tempfile.mkstemp(dir=self.path.parent,prefix='.cvexcel_',suffix='.xlsx');os.close(fd)
                    with zipfile.ZipFile(temporary,'w') as output:
                        for entry in source.infolist():output.writestr(entry,E.tostring(sheet,xml_declaration=True,encoding='UTF-8') if entry.filename=='xl/worksheets/sheet1.xml' else source.read(entry.filename))
                with zipfile.ZipFile(temporary) as check:
                    if check.testzip():raise ValueError('فشل التحقق من ملف Excel المؤقت.')
                with open(temporary,'r+b') as f:os.fsync(f.fileno())
                state['sha256']=digest(temporary)
                pending=self.manifest.with_suffix(self.manifest.suffix+'.pending');atomic_json(pending,state)
                release()
                if digest(self.path)!=self.state['sha256']:raise ValueError('تعارض Excel قبل اعتماد الحفظ.')
                os.replace(temporary,self.path);temporary=None
                self.state=state
                os.replace(pending,self.manifest)
        finally:
            if temporary:Path(temporary).unlink(missing_ok=True)
            if reservation is not None:reservation.close();lock.unlink(missing_ok=True)

class SaveWorker(QThread):
    result=Signal(object)
    def __init__(self,store,payload,parent):super().__init__(parent);self.store=store;self.payload=payload
    def run(self):
        try:self.store.update(*self.payload);self.result.emit(None)
        except Exception as exc:self.result.emit(str(exc))

class RegionAutosave(QObject):
    status=Signal(str)
    saved=Signal()
    failed=Signal(str)
    def __init__(self,parent):
        super().__init__(parent);self.store=None;self.revision=0;self.persisted=0;self.payload=None;self.worker=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(500);self.timer.timeout.connect(self.flush)
    @property
    def dirty(self):return self.revision!=self.persisted or self.worker is not None
    def bind(self,store):
        if self.dirty:raise RuntimeError('انتظر حفظ Excel قبل تبديل الجلسة.')
        self.store=store
    def queue(self,regions,columns,metadata):
        self.payload=copy.deepcopy((regions,columns,metadata));self.revision+=1
        self.status.emit('Excel: تعديلات بانتظار الحفظ' if self.store else 'Excel: غير مرتبط بملف جلسة؛ التعديلات باقية في الواجهة')
        if self.store:self.timer.start()
    def flush(self):
        self.timer.stop()
        if self.worker or not self.dirty:return
        if not self.store:self.failed.emit('لا يوجد ملف Excel مؤكد الارتباط بالجلسة الحالية؛ لم يُنشأ ملف فارغ.');return
        self.status.emit('Excel: جارٍ الحفظ…');revision=self.revision
        worker=SaveWorker(self.store,self.payload,self);self.worker=worker
        result=[];worker.result.connect(result.append)
        def finished():
            error=result[0] if result else 'لم يكتمل عامل الحفظ'
            self.worker=None;worker.deleteLater()
            if error:self.status.emit('Excel: تعذر الحفظ — '+error);self.failed.emit(error)
            else:
                self.persisted=revision
                if self.revision!=revision:self.timer.start(0)
                else:self.status.emit('Excel: تم الحفظ — '+str(self.store.path));self.saved.emit()
        worker.finished.connect(finished);worker.start()
