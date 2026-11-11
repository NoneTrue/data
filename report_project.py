"""Portable explicit .cvreport save/load, bounded ZIP; no settings/autosave."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import zipfile
from PIL import Image
from report_document import ReportDocument
from report_assets import AssetStore,MAX_EDGE,MAX_TOTAL_PIXELS
from report_cancel import checkpoint,cancellable,commit_file
from storage_manager import validate_output_path

MAX_TOTAL=512*1024*1024
MAX_JSON=24*1024*1024
MAX_FILES=10005

def encoded(value):return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')
@cancellable
def load_project(path):
    assets=AssetStore()
    with zipfile.ZipFile(path) as z:
        infos=z.infolist()
        if len(infos)>MAX_FILES or sum(i.file_size for i in infos)>MAX_TOTAL:raise ValueError('حجم مشروع التقرير يتجاوز الحدود')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names):raise ValueError('ملفات مكررة في المشروع')
        for i in infos:
            p=PurePosixPath(i.filename)
            if p.is_absolute() or '..' in p.parts or '\\' in i.filename or ':' in i.filename or stat.S_ISLNK(i.external_attr>>16):raise ValueError('مسار أرشيف غير آمن')
            if i.file_size and i.compress_size==0:raise ValueError('حجم مضغوط غير صالح')
        for key in ['manifest.json','document.json']:
            if key not in names or z.getinfo(key).file_size>MAX_JSON:raise ValueError('وصف مشروع مفقود أو كبير')
        manifest=json.loads(z.read('manifest.json'))
        if manifest.get('format')!='ChangeVision.cvreport' or manifest.get('version')!=1:raise ValueError('إصدار مشروع غير مدعوم')
        expected=manifest.get('files')
        if not isinstance(expected,dict) or set(expected)!=(set(names)-{'manifest.json'}):raise ValueError('قائمة ملفات غير متطابقة')
        for name,info in expected.items():
            checkpoint('project_verify')
            data=z.read(name)
            if len(data)!=info['bytes'] or hashlib.sha256(data).hexdigest()!=info['sha256']:raise ValueError('تلف في مكونات التقرير: '+name)
        doc=ReportDocument(json.loads(z.read('document.json')))
        for key,a in doc.to_dict()['assets'].items():
            checkpoint('project_asset')
            name='assets/'+key+'.png'
            if name not in expected:raise ValueError('مورد صورة مفقود')
            payload=z.read(name)
            if hashlib.sha256(payload).hexdigest()!=a['sha256']:raise ValueError('بصمة صورة غير متطابقة')
            with Image.open(io.BytesIO(payload)) as im:
                if im.format!='PNG' or im.width*im.height>MAX_EDGE**2:raise ValueError('صورة مضمنة غير آمنة')
                dimensions=list(im.size);im.verify()
            if dimensions!=a.get('dimensions'):raise ValueError('أبعاد مورد غير متطابقة')
            assets.pixel_sizes[key]=dimensions[0]*dimensions[1]
            if sum(assets.pixel_sizes.values())>MAX_TOTAL_PIXELS:raise ValueError('تجاوز ميزانية بكسلات التقرير')
            assets.payloads[key]=payload
        return doc,assets

@cancellable
def save_project(path,document,assets,overwrite=False):
    document.validate();path=validate_output_path(path,create_parent=False).resolve()
    if path.suffix.lower()!='.cvreport':raise ValueError('امتداد مشروع التقرير يجب أن يكون .cvreport')
    # Never overwrite a source, even if the user chooses its path.
    sources=[a.get('source_ref') for a in document.to_dict()['assets'].values()]
    if any(s and Path(s).resolve()==path for s in sources):raise ValueError('لا يمكن استبدال ملف المصدر')
    if path.exists() and not overwrite:raise FileExistsError(str(path))
    if not path.parent.is_dir():raise FileNotFoundError(str(path.parent))
    files={'document.json':encoded(document.to_dict())}
    if len(files['document.json'])>MAX_JSON:raise ValueError('بيانات التقرير كبيرة')
    for key,a in document.to_dict()['assets'].items():
        checkpoint('project_asset_save')
        payload=assets.payloads.get(key)
        if payload is None or hashlib.sha256(payload).hexdigest()!=a['sha256']:raise ValueError('مورد غير متوفر أو تغيرت بصمته')
        files['assets/'+key+'.png']=payload
    if sum(map(len,files.values()))>MAX_TOTAL:raise ValueError('المشروع أكبر من حد الحفظ')
    manifest={'format':'ChangeVision.cvreport','version':1,'files':{n:{'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in files.items()}}
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent);os.close(fd)
    try:
        with zipfile.ZipFile(temp,'w',zipfile.ZIP_DEFLATED) as z:
            for n,b in files.items():
                checkpoint('project_write')
                with z.open(n,'w') as member:
                    for offset in range(0,len(b),65536):checkpoint('project_write_chunk');member.write(b[offset:offset+65536])
            z.writestr('manifest.json',encoded(manifest))
        with open(temp,'rb+') as stream:stream.flush();os.fsync(stream.fileno())
        load_project(temp) # Verify before committing; previous destination intact on failure.
        if path.exists() and not overwrite:raise FileExistsError(str(path))
        checkpoint('project_before_commit');commit_file(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
