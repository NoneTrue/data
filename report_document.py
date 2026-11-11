"""Versioned, independent report model. No application/storage/Qt imports."""
from __future__ import annotations
import copy
import datetime
import json
import uuid

SCHEMA_VERSION = 1
SECTION_TYPES = [('cover','الغلاف'),('overview','ملخص المشروع'),('metrics','مؤشرات التحليل'),
 ('sources','الصور والمصادر'),('targets','قائمة الأهداف'),('details','تفاصيل الأهداف'),
 ('images','صور الأهداف'),('notes','الملاحظات والاستنتاجات'),('technical','البيانات التقنية ومصادرها')]

def uid(): return str(uuid.uuid4())
def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def block(kind, **values):return {'uid':uid(),'kind':kind,'visible':True,'style':{},**copy.deepcopy(values)}
def section(kind, title=None):return {'uid':uid(),'kind':kind,'title':title or dict(SECTION_TYPES).get(kind,'قسم مخصص'),'visible':True,'blocks':[]}

class ReportDocument:
    def __init__(self, data=None):
        self._data=copy.deepcopy(data) if data is not None else {'schema_version':SCHEMA_VERSION,
          'report_uid':uid(),'revision':0,'created_at':now(),'modified_at':now(),'title':'تقرير ChangeVision 2026',
          'analysis_snapshot':{},'targets':[],'assets':{},'sections':[section(k,t) for k,t in SECTION_TYPES],
          'page_settings':{'size':'A4','margin_mm':17},'theme':{'font':'Arial','size':11,'color':'#183c57'},'warnings':[]}
        if data is None:self._data['sections'][0]['blocks']=[block('heading',text=self._data['title'],locked=False)]
        self._data.setdefault('default_sections',copy.deepcopy(self._data['sections']))
        self._data.setdefault('target_selection',{t['uid']:True for t in self._data['targets']})
        self._migrate_report_links()
        self.validate()
    def _migrate_report_links(self):
        # Older v1 drafts: use explicit field provenance/unique saved IDs,
        # never row order or visual resemblance. Ambiguous IDs stay unbound.
        targets=self._data.get('targets',[]);known={t.get('uid') for t in targets};by_id={}
        for t in targets:by_id.setdefault(str(t.get('display_id')),[]).append(t.get('uid'))
        for groups in [self._data.get('sections',[]),self._data.get('default_sections',[])]:
            for s in groups:
                heading=None
                for b in s.get('blocks',[]):
                    if b.get('kind')=='heading':heading=b
                    source=b.get('source','')
                    if isinstance(source,str) and source.startswith('target:') and source[7:] in known:
                        b.setdefault('target_uid',source[7:])
                        if heading is not None:heading.setdefault('target_uid',source[7:])
                    if b.get('kind')=='table' and source=='included_targets' and 'target_row_uids' not in b:
                        ci=next((i for i,c in enumerate(b.get('columns',[])) if c.get('key') in ['id','target_id']),None)
                        if ci is not None:
                            links=[by_id.get(str(row[ci]),[]) for row in b.get('rows',[])]
                            if all(len(found)==1 for found in links):b['target_row_uids']=[found[0] for found in links]
    def to_dict(self):return copy.deepcopy(self._data)
    @property
    def revision(self):return self._data['revision']
    @property
    def report_uid(self):return self._data['report_uid']
    def sections_for_view(self):
        # Internal read-only view: controller always replaces the document on edit.
        return self._data['sections']
    def targets_for_view(self):return self._data['targets']
    def warning_count(self):return len(self._data.get('warnings',[]))
    @property
    def analysis_snapshot(self):return copy.deepcopy(self._data['analysis_snapshot'])
    def validate(self):
        d=self._data
        if d.get('schema_version')!=SCHEMA_VERSION:raise ValueError('إصدار مشروع التقرير غير مدعوم؛ لن يستبدل الملف.')
        for k,t in [('report_uid',str),('revision',int),('title',str),('analysis_snapshot',dict),('targets',list),('assets',dict),('sections',list)]:
            if not isinstance(d.get(k),t):raise ValueError('حقل تقرير غير صالح: '+k)
        if d['revision']<0:raise ValueError('مراجعة تقرير غير صالحة')
        seen=set()
        def unique(value):
            if not isinstance(value,str) or not value or len(value)>200 or value in seen:raise ValueError('هوية مكررة أو غير صالحة')
            seen.add(value)
        targets=set()
        for target in d['targets']:
            unique(target['uid']);targets.add(target['uid'])
        selection=d.get('target_selection',{})
        if not isinstance(selection,dict) or set(selection)-targets or any(not isinstance(v,bool) for v in selection.values()):raise ValueError('اختيار أهداف التقرير غير صالح')
        for key,a in d['assets'].items():
            unique(key)
            if a.get('uid')!=key:raise ValueError('هوية مورد غير متطابقة')
        placements=set()
        for s in d['sections']:
            unique(s['uid'])
            if not isinstance(s.get('title'),str) or not isinstance(s.get('visible'),bool) or not isinstance(s.get('blocks'),list):raise ValueError('قسم غير صالح')
            for b in s['blocks']:
                unique(b['uid'])
                if b.get('target_uid') is not None and b['target_uid'] not in targets:raise ValueError('عنصر مرتبط بهدف غير موجود')
                if b.get('kind') not in ['heading','paragraph','note','conclusion','field','table','image']:raise ValueError('نوع عنصر غير مدعوم')
                if b['kind']=='table':
                    if not isinstance(b.get('columns'),list) or not isinstance(b.get('rows'),list):raise ValueError('جدول غير صالح')
                    keys=[c['key'] for c in b['columns']]
                    if len(set(keys))!=len(keys) or any(len(r)!=len(keys) for r in b['rows']):raise ValueError('أعمدة أو صفوف غير متطابقة')
                if b['kind']=='image':
                    if b.get('target_uid') is not None and b['target_uid'] not in targets:raise ValueError('صورة مرتبطة بهدف غير موجود')
                    if b.get('asset_uid') is not None and b['asset_uid'] not in d['assets']:raise ValueError('مرجع صورة غير موجود')
                    if b.get('original_asset_uid') is not None and b['original_asset_uid'] not in d['assets']:raise ValueError('مرجع أصل الصورة غير موجود')
                    placement=b.get('placement_uid');unique(placement);placements.add(placement)
                    if not isinstance(b.get('annotations',[]),list):raise ValueError('طبقات رسم غير صالحة')
                    for a in b.get('annotations',[]):
                        if not isinstance(a,dict) or a.get('type') not in ['line','arrow','rectangle','circle','ellipse','freehand','polygon','text']:raise ValueError('نوع رسم غير مدعوم')
                        pts=a.get('points',[])
                        if not isinstance(pts,list) or len(pts)>100000:raise ValueError('نقاط الرسم غير صالحة')
                        for pt in pts:
                            if not isinstance(pt,(list,tuple)) or len(pt)!=2 or any(not isinstance(v,(float,int)) or not __import__('math').isfinite(v) or abs(v)>1e7 for v in pt):raise ValueError('إحداثيات رسم غير صالحة')
        json.dumps(d,ensure_ascii=False,allow_nan=False)
        return True

def report_target_name_column(table, column):
    """A report-local name cell; the captured target record stays immutable."""
    return table.get('source') == 'included_targets' and column.get('key') == 'target_name'


class ReportController:
    """All UI mutations use this boundary; derived content is read-only."""
    def __init__(self, document=None):
        self.document=document or ReportDocument();self.undo_stack=[];self.redo_stack=[]
        self.saved_state=self.document._data
    @property
    def dirty(self):return self.document._data!=self.saved_state
    def mark_saved(self):self.saved_state=self.document._data
    def mark_saved_document(self,document):self.saved_state=document._data
    def mutate(self, operation):
        before=self.document._data;after=copy.deepcopy(before);operation(after)
        # Analysis and stable target records cannot be edited by report commands.
        if after['analysis_snapshot']!=before['analysis_snapshot'] or after['targets']!=before['targets']:raise ValueError('بيانات التحليل محمية')
        if after==before:return
        after['revision']=before['revision']+1;after['modified_at']=now();candidate=ReportDocument(after)
        self.undo_stack.append(before);self.undo_stack=self.undo_stack[-100:];self.redo_stack.clear();self.document=candidate
    def undo(self):
        if self.undo_stack:self.redo_stack.append(self.document.to_dict());self.document=ReportDocument(self.undo_stack.pop())
    def redo(self):
        if self.redo_stack:self.undo_stack.append(self.document.to_dict());self.document=ReportDocument(self.redo_stack.pop())
    def add_section(self,title):
        s=section('custom',title);self.mutate(lambda d:d['sections'].append(s));return s['uid']
    def add_block(self,section_uid,item):
        self.mutate(lambda d:next(s for s in d['sections'] if s['uid']==section_uid)['blocks'].append(copy.deepcopy(item)))
    def edit_block(self,block_uid,values):
        def change(d):
            b=next(b for s in d['sections'] for b in s['blocks'] if b['uid']==block_uid)
            if any(k in values for k in ['uid','placement_uid','target_uid','kind']):raise ValueError('هوية العنصر محمية')
            if b.get('locked') and any(k in values for k in ['value','rows','text','source','columns']):raise ValueError('قيمة التحليل محمية؛ أضف تعليقًا مستقلًا')
            b.update(copy.deepcopy(values))
        self.mutate(change)
    def restore_sections(self):
        def change(d):
            present={b['uid'] for sec in d['sections'] for b in sec['blocks']}
            for kind,title in SECTION_TYPES:
                found=next((s for s in d['sections'] if s['kind']==kind),None)
                if found:
                    found['visible']=True
                    template=next((s for s in d.get('default_sections',[]) if s['kind']==kind),None)
                    if template:
                        for b in template['blocks']:
                            if b['uid'] not in present:found['blocks'].append(copy.deepcopy(b));present.add(b['uid'])
                else:
                    template=next((s for s in d.get('default_sections',[]) if s['kind']==kind),section(kind,title))
                    restored=copy.deepcopy(template);present={b['uid'] for sec in d['sections'] for b in sec['blocks']}
                    restored['blocks']=[b for b in restored['blocks'] if b['uid'] not in present];restored['visible']=True
                    d['sections'].append(restored)
        self.mutate(change)
    def reset_design(self):
        def change(d):
            d['theme']={'font':'Arial','size':11,'color':'#183c57'};d['page_settings']={'size':'A4','margin_mm':17}
            for s in d['sections']:
                for b in s['blocks']:
                    b['style']={}
                    if b['kind']=='image':b['width_mm']=140
                    if b['kind']=='table':
                        for c in b['columns']:c['width']=1
        self.mutate(change)
    def edit_table(self,block_uid,columns,rows):
        def change(d):
            b=next(b for s in d['sections'] for b in s['blocks'] if b['uid']==block_uid)
            if b['kind']!='table':raise ValueError('ليس جدولًا')
            source_count=b.get('source_row_count',len(b['rows']) if b.get('locked') else 0)
            if len(rows)<source_count:raise ValueError('لا تحذف صف تحليل أصلي')
            newkeys=[c['key'] for c in columns]
            for i,c in enumerate(b['columns']):
                if not c.get('locked'):continue
                if c['key'] not in newkeys:raise ValueError('لا تحذف عمود تحليل أصلي')
                ni=newkeys.index(c['key'])
                if not report_target_name_column(b,c) and any(rows[r][ni]!=b['rows'][r][i] for r in range(source_count)):raise ValueError('خلايا التحليل مقفلة')
                columns[ni]['locked']=True
            b.update(columns=copy.deepcopy(columns),rows=copy.deepcopy(rows),source_row_count=source_count)
        self.mutate(change)
    def toggle_target(self,target_uid):
        def change(d):
            if target_uid not in {t['uid'] for t in d['targets']}:raise ValueError('هدف غير موجود')
            if any(b.get('source')=='included_targets' and 'target_row_uids' not in b for s in d['sections'] for b in s['blocks']):raise ValueError('ربط جدول قديم غير محسوم؛ لا يستبعد الهدف بالتخمين')
            d['target_selection'][target_uid]=not d['target_selection'].get(target_uid,True)
        self.mutate(change)

def item_included(data,item):
    target=item.get('target_uid')
    return item.get('visible',True) and (not target or data['target_selection'].get(target,True))

def rows_for_report(data,item):
    ids=item.get('target_row_uids') or []
    return [row for i,row in enumerate(item['rows']) if i>=len(ids) or data['target_selection'].get(ids[i],True)]
