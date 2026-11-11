"""Read-only snapshot adapter. Never imports main, targets, settings or engines."""
from __future__ import annotations
import copy
import math
from pathlib import Path
from report_document import ReportDocument,block,uid
from report_assets import AssetStore
from report_cancel import checkpoint

LABELS={'operation_name':'اسم العملية','title':'العنوان','operation_start':'بدء العملية','operation_end':'انتهاء العملية',
 'before_date':'تاريخ الصورة قبل','after_date':'تاريخ الصورة بعد','before_location':'موقع الصورة قبل','after_location':'موقع الصورة بعد',
 'longitude':'Longitude','latitude':'Latitude','location':'الموقع','target_name':'اسم الهدف','target_state':'حالة الهدف',
 'classification':'تصنيف التحليل','description':'الوصف','notes':'الملاحظات','changed_pixels':'البكسلات المتغيرة',
 'total_pixels':'إجمالي البكسلات','changed_percent':'نسبة التغير %','changed_area_map_units2':'مساحة التغير بوحدات الخريطة المربعة',
 'similarity_percent':'نسبة التشابه %','candidate_count':'عدد المرشحين','accepted_count':'المقبول','review_count':'قيد المراجعة',
 'rejected_count':'المرفوض','object_count':'مجموعات التغيير','appearance_count':'ظهور','disappearance_count':'اختفاء',
 'structural_count':'تغير بنيوي','stable_count':'مستقر','verified_change_count':'تغير مؤكد'}

def plain(value):
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):return value if math.isfinite(value) else None
    if isinstance(value,dict):return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [plain(v) for v in value]
    if hasattr(value,'tolist'):
        if getattr(value,'size',0)>100000:raise ValueError('مصفوفة كبيرة غير مناسبة لـsnapshot التقرير؛ لا تحمل صورة التحليل كقيمة حقل')
        return plain(value.tolist())
    if hasattr(value,'item'):return plain(value.item())
    return str(value)

def from_snapshot(snapshot):
    snap=plain(snapshot or {});doc=ReportDocument();d=doc.to_dict();d['analysis_snapshot']=copy.deepcopy(snap)
    assets=AssetStore();sections={s['kind']:s for s in d['sections']}
    meta=snap.get('report_metadata') or {};d['title']=meta.get('operation_name') or d['title']
    sections['cover']['blocks']=[block('heading',text=d['title'],locked=False)]
    for key,value in meta.items():
        if not key.startswith('_'):sections['overview']['blocks'].append(block('field',label=LABELS.get(key,key),value=value,locked=True,source=(meta.get('_sources') or {}).get(key,'metadata'),source_key=key))
    for key,value in (snap.get('change_summary') or {}).items():
        unit=''
        if key=='changed_area_map_units2':
            crs=str((snap.get('image1') or {}).get('crs') or '')
            unit='درجة مربعة؛ ليست m²' if '4326' in crs else 'مربع وحدة CRS؛ تحقق من المرجع'
        sections['metrics']['blocks'].append(block('field',label=LABELS.get(key,key),value=value,unit=unit,locked=True,source='analysis.change_summary.'+key))
    for name in ['image1','image2']:
        info=snap.get(name) or {}
        for key,value in info.items():sections['sources']['blocks'].append(block('field',label=name+'.'+key,value=value,locked=True,source=name))
        if info.get('path') and not info.get('name'):sections['sources']['blocks'].append(block('field',label=name+'.name',value=Path(info['path']).name,locked=True,source='derived from path; not captured metadata'))
    columns=copy.deepcopy(snap.get('region_columns') or [{'key':'target_id','title':'ID'},{'key':'target_name','title':'اسم الهدف'},{'key':'longitude','title':'Longitude'},{'key':'latitude','title':'Latitude'}])
    columns=[{**c,'visible':bool(c.get('visible',True)),'locked':True,'width':float(c.get('width',1) or 1)} for c in columns if c.get('key')!='navigate']
    rows=[];image_blocks=[];identities=set()
    for target in snap.get('regions') or []:
        checkpoint('snapshot_target')
        if not target.get('report_included',False):continue
        target_uid=str(target.get('target_uid') or 'report-local-'+uid())
        if target_uid in identities:raise ValueError('target_uid مكرر؛ تعذر إثبات ربط الأهداف')
        identities.add(target_uid)
        d['targets'].append({'uid':target_uid,'display_id':target.get('id',target.get('object_id')),'source_target_uid':target.get('target_uid'),'record':copy.deepcopy(target)})
        def value(key):
            if key in ['id','target_id']:return target.get('id',target.get('object_id'))
            own=(target.get('excel_values') or {}).get(key,target.get(key))
            return own if own not in ('',None) else meta.get(key,own)
        rows.append([value(c['key']) for c in columns])
        label=target.get('target_name') or 'غير مدخل'
        sections['details']['blocks'].append(block('heading',text=f"ID {target.get('id','غير متوفر')} — {label}",locked=True,target_uid=target_uid))
        for key in ['target_name','target_state','classification','description','notes','longitude','latitude','before_date','after_date','location']:
            sections['details']['blocks'].append(block('field',label=LABELS.get(key,key),value=target.get(key),locked=True,source='target:'+target_uid,source_key=key,target_uid=target_uid))
        entries=[copy.deepcopy(p) for p in target.get('report_images') or []]
        for role in ['before','after']:
            layer=(target.get('image_layers') or {}).get(role) or {}
            path=layer.get('original_path') or target.get(role+'_image') or layer.get('working_path') or layer.get('composite_path') or target.get(role+'_edited_image')
            if path:entries.append({'path':path,'source':role,'source_label':role,'report_order':len(entries)+1,'layer_state':layer})
        for entry in entries:
            checkpoint('snapshot_image')
            asset=None;original_asset=None;composite_asset=None;error=None;annotations=[];layer=entry.get('layer_state') or {};original_available=True;editable=True
            try:
                chosen=entry.get('path') or ''
                original_path=layer.get('original_path') or target.get(str(entry.get('source'))+'_image') or (chosen if not layer else None)
                if original_path and Path(original_path).is_file():original_asset=assets.ingest(original_path);d['assets'][original_asset['uid']]=original_asset
                original_available=original_asset is not None
                working=layer.get('working_path')
                if working and Path(working).is_file():asset=assets.ingest(working);d['assets'][asset['uid']]=asset
                elif original_asset is not None:asset=original_asset
                else:
                    fallback=layer.get('composite_path') or chosen
                    asset=assets.ingest(fallback);d['assets'][asset['uid']]=asset;editable=False
                    d['warnings'].append({'target_uid':target_uid,'error':'المركب فقط متاح؛ الأصل ونسخة العمل غير متوفرين، لا تعاد طبقات قديمة فوق صورة مسطحة'})
                if layer.get('composite_path') and Path(layer['composite_path']).is_file():composite_asset=assets.ingest(layer['composite_path']);d['assets'][composite_asset['uid']]=composite_asset
                annotations=copy.deepcopy(layer.get('annotations') or []) if editable else []
                sx=asset['dimensions'][0]/asset['original_dimensions'][0];sy=asset['dimensions'][1]/asset['original_dimensions'][1]
                for annotation in annotations:
                    annotation['points']=[(float(x)*sx,float(y)*sy) for x,y in annotation.get('points',[])]
                    for prop in ['size','font_size','arrow_size']:
                        if prop in annotation:annotation[prop]=max(1,float(annotation[prop])*(sx+sy)/2)
                    if annotation.get('type')=='circle' and annotation.get('report_schema')!=1:annotation.update(type='ellipse',legacy_type='circle')
                    annotation.setdefault('id',uid());annotation['report_schema']=1
            except Exception as exc:error=str(exc);d['warnings'].append({'target_uid':target_uid,'source':entry.get('path'),'error':error})
            image_blocks.append(block('image',placement_uid=uid(),target_uid=target_uid,asset_uid=asset['uid'] if asset else None,
              role=entry.get('source') or 'manual',caption=f"ID {target.get('id','غير متوفر')} — {entry.get('source_label') or entry.get('source') or 'غير متوفر'}",
              source_ref=entry.get('path'),order=entry.get('report_order',0),width_mm=140,variant='annotated' if composite_asset or (asset and not original_available) else 'original',
              annotations=annotations,original_asset_uid=original_asset['uid'] if original_asset else None,original_available=original_available,
              composite_asset_uid=composite_asset['uid'] if composite_asset else None,
              layer_adjustments={k:layer.get(k,v) for k,v in [('brightness',0),('contrast',100),('sharpness',0)]} if editable else {},missing_error=error))
    sections['targets']['blocks'].append(block('table',columns=columns,rows=rows,locked=True,source='included_targets',target_row_uids=[t['uid'] for t in d['targets']]))
    if not d['targets']:sections['targets']['blocks'].append(block('note',text='لا توجد أهداف مدرجة في هذا التقرير',locked=False))
    d['target_selection']={t['uid']:True for t in d['targets']}
    sections['images']['blocks']=sorted(image_blocks,key=lambda b:float(b.get('order') or 0))
    for key in ['alignment','comparison','objects','settings']:
        if snap.get(key) is not None:sections['technical']['blocks'].append(block('field',label=key,value=snap[key],locked=True,source='analysis.'+key))
    d['default_sections']=copy.deepcopy(d['sections'])
    return ReportDocument(d),assets

def from_legacy_html(markup):
    """Compatibility only: retain text/tables/images without inventing target links."""
    from lxml import html as lhtml
    from urllib.parse import urlparse,unquote
    parser=lhtml.HTMLParser(no_network=True)
    root=lhtml.fromstring(markup,parser=parser);document=ReportDocument();data=document.to_dict();assets=AssetStore()
    data['sections']=[{'uid':uid(),'kind':'custom','title':'تقرير ChangeVision 2026','visible':True,'blocks':[]}];items=data['sections'][0]['blocks']
    def walk(element):
        tag=str(element.tag).lower()
        if tag in ['style','script','head','meta']:return
        if tag=='table':
            rows=[[ ''.join(cell.itertext()).strip() for cell in row.xpath('./th|./td')] for row in element.xpath('.//tr')]
            rows=[r for r in rows if r]
            if rows:
                width=max(map(len,rows));rows=[r+['']*(width-len(r)) for r in rows]
                items.append(block('table',columns=[{'key':str(i),'title':v or str(i+1),'visible':True,'locked':True,'width':1} for i,v in enumerate(rows[0])],rows=rows[1:],locked=True))
        elif tag in ['p','h1','h2','h3','pre']:
            text=''.join(element.itertext()).strip()
            if text:items.append(block('heading' if tag.startswith('h') else 'paragraph',text=text,locked=True))
            for image in element.xpath('.//img'):walk(image)
        elif tag=='img':
            url=element.get('src','');parsed=urlparse(url)
            if parsed.scheme not in ['','file']:data['warnings'].append({'source':url,'error':'مصدر غير محلي غير محمل'});return
            path=unquote(parsed.path) if parsed.scheme=='file' else url
            if path.startswith('/') and len(path)>2 and path[2]==':':path=path[1:]
            a=None
            try:a=assets.ingest(path);data['assets'][a['uid']]=a
            except Exception as exc:data['warnings'].append({'source':path,'error':str(exc)})
            items.append(block('image',asset_uid=a['uid'] if a else None,placement_uid=uid(),target_uid=None,source_ref=path,role='legacy-unspecified',caption='صورة من التقرير القديم؛ ربط الهدف غير متاح في HTML',width_mm=140,variant='original',annotations=[]))
        else:
            for child in element:walk(child)
    walk(root);data['default_sections']=copy.deepcopy(data['sections']);return ReportDocument(data),assets
