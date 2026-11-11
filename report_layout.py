"""Shared Qt page layout for interactive preview and PDF, from a fixed report."""
from __future__ import annotations
import html
import json
import math
from pathlib import Path
import os
import tempfile
import time
from collections import OrderedDict
from report_document import item_included,rows_for_report
from report_presentation import runs,groups,cover_counts,column_weight,BODY_SIZE,HEADING_SIZE
from report_cancel import checkpoint,commit_file,cancellable
from storage_manager import validate_output_path
from PySide6.QtCore import Qt,QRectF,QSizeF,QUrl
from PySide6.QtGui import QFont,QTextDocument,QTextOption,QImage,QPainter,QPdfWriter,QPageSize,QPageLayout,QColor,QTextCursor,QTextCharFormat,QTextBlockFormat,QTextFormat

WIDTH,HEIGHT=794.,1123.
MARGIN=64.
BODY_W,BODY_H=WIDTH-2*MARGIN,HEIGHT-2*MARGIN-30
def display(value):
    if value is None:return 'غير متوفر'
    if value=='':return 'غير مدخل'
    if isinstance(value,(dict,list)):return json.dumps(value,ensure_ascii=False,indent=2)
    return str(value)
def esc(value):return html.escape(display(value))
def latin(value):
    return bool(__import__('re').match(r'^[\s\d.,eE+\-/%:×A-Za-z_\\/]+$',str(value)))
def formatted(value):
    return ''.join('<span dir="ltr">&#x2066;'+esc(part)+'&#x2069;</span>' if latin(part) and part.strip() else esc(part) for part in runs(display(value)))

def table_bands(item):
    """Preserve wide tables without crushing all source columns onto A4."""
    cols=[(i,c) for i,c in enumerate(item['columns']) if c.get('visible',True)]
    if len(cols)<=5:return [cols] if cols else []
    identity=next((c for c in cols if c[1]['key'] in ['id','target_id']),cols[0])
    rest=[c for c in cols if c!=identity]
    return [[identity]+rest[i:i+4] for i in range(0,len(rest),4)]

def image_payload(item,assets):
    checkpoint('image_payload')
    variant=item.get('variant','original')
    if variant=='original' and item.get('original_available') is False:return None
    if variant=='annotated' and item.get('composite_asset_uid'):return assets.payloads.get(item['composite_asset_uid'])
    key=(item.get('original_asset_uid') or item.get('asset_uid')) if variant=='original' else item.get('asset_uid')
    original=assets.payloads.get(key)
    if not original:return None
    if variant=='original':return original
    from image_editor import draw_report_annotation,adjusted_rgb
    if item.get('layer_adjustments'):
        import numpy as np
        from PIL import Image
        import io
        with Image.open(io.BytesIO(original)) as im:rgb=np.asarray(im.convert('RGB')).copy()
        rgb=adjusted_rgb(rgb,**item['layer_adjustments']);image=QImage(rgb.data,rgb.shape[1],rgb.shape[0],rgb.strides[0],QImage.Format_RGB888).copy().convertToFormat(QImage.Format_ARGB32)
    else:image=QImage.fromData(original).convertToFormat(QImage.Format_ARGB32)
    p=QPainter(image);p.setRenderHint(QPainter.Antialiasing)
    try:
        for a in item['annotations']:checkpoint('image_annotation');draw_report_annotation(p,a)
    finally:p.end()
    from PySide6.QtCore import QBuffer,QIODevice
    b=QBuffer();b.open(QIODevice.WriteOnly);image.save(b,'PNG');return bytes(b.data())

class ReportLayout:
    def __init__(self,document,assets,progress=None):
        checkpoint('layout_start')
        started=time.perf_counter();self.timings={}
        self.data=document.to_dict();self.timings['snapshot_copy_seconds']=time.perf_counter()-started
        self.doc=QTextDocument();self.doc.setDefaultFont(QFont('Arial',BODY_SIZE))
        option=self.doc.defaultTextOption();option.setTextDirection(Qt.RightToLeft);option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere);self.doc.setDefaultTextOption(option)
        self.doc.setDocumentMargin(0);self.doc.setPageSize(QSizeF(BODY_W,BODY_H));self.anchors={};self.items={}
        chunks=['<html dir="rtl"><head><meta charset="utf-8"><style>body{font-family:Arial;color:#183c57;font-size:12pt} h1{font-size:32pt} h2{font-size:19pt;background-color:#e8eff5;margin-top:18px;margin-bottom:12px;page-break-after:avoid} p{margin-top:5px;margin-bottom:8px} table{border-collapse:collapse;width:100%;font-size:11pt} td,th{padding:6px;border:1px solid #bdccd5} th{background-color:#183c57;color:white}</style></head><body>']
        resources=[]
        boundaries=[]
        first_visible=next((s for s in self.data['sections'] if s['visible']),None)
        self.has_cover=bool(first_visible and first_visible['kind']=='cover')
        for s in self.data['sections']:
            checkpoint('layout_section')
            if not s['visible']:continue
            dark_cover=s['kind']=='cover' and self.has_cover
            if dark_cover:chunks.append('<p dir="ltr" style="color:white;font-size:15pt;margin-bottom:60px">CHANGEVISION <b>2026</b></p>')
            chunks.append(('<p dir="rtl" align="right" style="font-size:11pt;color:#bbdde8">' if dark_cover else '<p dir="rtl" align="right" style="font-size:19pt;font-weight:700;background-color:#e8eff5;margin-top:18px;margin-bottom:12px">')+'<a name="'+s['uid']+'"></a>'+esc(s['title'])+'</p>');self.items[s['uid']]=s
            boundaries.append(len(chunks))
            for group_kind,items in groups(b for b in s['blocks'] if item_included(self.data,b)):
                checkpoint('layout_block')
                if group_kind in ['fields','target_card']:
                    card_heading=items[0] if group_kind=='target_card' else None
                    if card_heading:items=items[1:];self.items[card_heading['uid']]=card_heading
                    chunks.append('<table dir="ltr" width="100%" cellspacing="0" cellpadding="5">')
                    if card_heading:chunks.append('<thead><tr><td colspan="4" bgcolor="#e8f0f5" dir="rtl"><p dir="rtl" align="right" style="font-size:16pt;font-weight:700;color:#183c57"><a name="'+card_heading['uid']+'"></a>'+formatted(card_heading.get('text',''))+'</p></td></tr></thead>')
                    for offset in range(0,len(items),2):
                        checkpoint('layout_fields')
                        chunks.append('<tr>')
                        pair=items[offset:offset+2]
                        if len(pair)==1:chunks.append('<td width="34%"></td><td width="16%"></td>')
                        for item in reversed(pair):
                            self.items[item['uid']]=item
                            anchor='<a name="'+item['uid']+'"></a>'
                            st=item.get('style') or {}
                            field_css='font-size:'+str(max(7,min(48,float(st.get('size',11)))))+'pt;color:'+html.escape(st.get('color','#183c57'),quote=True)+';font-family:'+html.escape(st.get('font','Arial'),quote=True)+';'
                            if st.get('bold'):field_css+='font-weight:bold;'
                            if st.get('italic'):field_css+='font-style:italic;'
                            value='<span style="'+field_css+'">'+formatted(item.get('value'))+(' '+esc(item['unit']) if item.get('unit') else '')+'</span>'
                            source=item.get('source','مستخدم')
                            if not str(source).startswith('target:'):value+='<br><span style="font-size:8pt;color:#536b7a">المصدر: '+formatted(source)+'</span>'
                            chunks.append('<td width="34%" bgcolor="white">'+value+'</td><td width="16%" dir="rtl" bgcolor="#e8f0f5">'+anchor+'<b>'+esc(item.get('label',''))+'</b></td>')
                        chunks.append('</tr>')
                        # Finish bounded native tables; no giant table handed to Qt's parser.
                        if (offset+2)%12==0 and offset+2<len(items):chunks.append('</table>');boundaries.append(len(chunks));chunks.append('<table dir="ltr" width="100%">')
                    chunks.append('</table>')
                    sources=list(dict.fromkeys(i.get('source','') for i in items if str(i.get('source','')).startswith('target:')))
                    if sources:chunks.append('<p style="font-size:8pt;color:#536b7a">مرجع بيانات الهدف: '+formatted('، '.join(sources))+'</p>')
                    boundaries.append(len(chunks));continue
                b=items[0]
                self.items[b['uid']]=b;anchor='<a name="'+b['uid']+'"></a>'
                style=b.get('style') or {};size=max(7,min(48,float(style.get('size',16 if b['kind']=='heading' else BODY_SIZE))))
                color=style.get('color','#ffffff' if dark_cover else '#183c57');font=style.get('font','Arial');alignment=style.get('align','right')
                if s['kind']=='cover' and b['kind']=='heading' and 'size' not in style:size=32
                css=f'font-family:{html.escape(font,quote=True)};font-size:{size}pt;color:{html.escape(color,quote=True)};text-align:{alignment};'
                if style.get('bold'):css+='font-weight:bold;'
                elif b['kind']=='heading':css+='font-weight:bold;'
                if style.get('italic'):css+='font-style:italic;'
                kind=b['kind']
                if kind in ['heading','paragraph','note','conclusion']:
                    chunks.append(f'<p dir="rtl" align="{alignment}" style="{css}">'+anchor+formatted(b.get('text','')).replace('\n','<br>')+'</p>')
                elif kind=='field':chunks.append('<p>'+anchor+'<b>'+esc(b.get('label',''))+': </b>'+formatted(b.get('value'))+(' '+esc(b['unit']) if b.get('unit') else '')+'<br><small>المصدر: '+esc(b.get('source','مستخدم'))+'</small></p>')
                elif kind=='table':
                    chunks.append('<p>'+anchor+'</p>')
                    for band_index,band in enumerate(table_bands(b)):
                        cols=list(reversed(band));header=['<table dir="ltr" width="100%"><thead><tr>']
                        total=sum(column_weight(c) for _,c in cols) or 1
                        for _,c in cols:header.append(f'<th width="{100*column_weight(c)/total:.2f}%">'+esc(c.get('title',c['key']))+'</th>')
                        header.append('</tr></thead><tbody>');chunks.extend(header)
                        table_rows=rows_for_report(self.data,b)
                        for row_index,row in enumerate(table_rows):
                            checkpoint('layout_table_row')
                            chunks.append('<tr>'+''.join('<td bgcolor="'+('#f1f6f9' if row_index%2 else '#ffffff')+'" dir="'+('ltr' if latin(row[i]) else 'rtl')+'">'+formatted(row[i])+'</td>' for i,c in cols)+'</tr>')
                            if (row_index+1)%75==0 and row_index+1<len(table_rows):
                                chunks.append('</tbody></table><p></p>');boundaries.append(len(chunks));chunks.extend(header)
                        chunks.append('</tbody></table><p></p>')
                elif kind=='image':
                    variants=['original','annotated'] if b.get('variant')=='both' else [b.get('variant','original')]
                    for variant_index,variant in enumerate(variants):
                        chunks.append('<p dir="rtl" align="center" style="page-break-before:always;font-size:14pt;margin-top:18px">'+(anchor if variant_index==0 else '<a name="'+b['uid']+'-variant"></a>')+'<b>'+formatted(b.get('caption',''))+'</b><br>')
                        payload=image_payload({**b,'variant':variant},assets)
                        if payload:
                            im=QImage.fromData(payload);url=QUrl('report-image:///'+b['placement_uid']+'/'+variant)
                            resources.append((url,im));width=min(BODY_W,float(b.get('width_mm',140))*96/25.4)
                            height=width*im.height()/max(1,im.width())
                            if height>BODY_H-160:width*= (BODY_H-160)/height;height=BODY_H-160
                            chunks.append(f'<img src="{url.toString()}" width="{width:.1f}" height="{height:.1f}"></p>')
                        else:chunks.append('الصورة غير متوفرة — '+esc(b.get('source_ref'))+'</p>')
                    if b.get('description'):chunks.append('<p>'+esc(b['description'])+'</p>')
                boundaries.append(len(chunks))
            if dark_cover:
                chunks.append('<p style="margin-top:45px"></p><table dir="rtl" width="100%"><tr>')
                for label,count in cover_counts(self.data):chunks.append('<td bgcolor="#294f68" align="center"><p style="font-size:27pt;color:white;font-weight:700">'+str(count)+'</p><p style="font-size:12pt;color:white">'+label+'</p></td>')
                chunks.append('</tr></table><p style="font-size:13pt;color:#bbdde8;margin-top:24px">تقرير تحليل التغيرات • الأهداف والصور والبيانات المرجعية</p>')
            if s['kind']=='cover':chunks.append('<p style="page-break-after:always">&#8203;</p>');boundaries.append(len(chunks))
        self.timings['prepare']=time.perf_counter()-started;tick=time.perf_counter()
        for url,im in resources:self.doc.addResource(QTextDocument.ImageResource,url,im)
        # Qt's monolithic setHtml holds Python's GIL even in QThread. Parse bounded
        # complete fragments and yield between them so GUI Python slots can run.
        css=chunks[0].split('<style>',1)[1].split('</style>',1)[0];self.doc.setDefaultStyleSheet(css)
        cursor=QTextCursor(self.doc);begin=1;fragments=[];pending=[];length=0
        for end in boundaries:
            part=''.join(chunks[begin:end]);begin=end
            pending.append(part);length+=len(part)
            if length>=12000 or len(pending)>=25 or 'page-break-after:always' in part:
                fragments.append(''.join(pending));pending=[];length=0
        if pending:fragments.append(''.join(pending))
        first_sent=False
        for index,fragment in enumerate(fragments):
            checkpoint('layout_fragment')
            if index:cursor.insertBlock(QTextBlockFormat(),QTextCharFormat())
            # insertHtml merges its first paragraph into the cursor's existing
            # block, losing that paragraph's alignment/page-break properties.
            # Consume the merge with a tiny neutral paragraph so the actual
            # first content block retains its authored layout (notably captions).
            native_start=time.perf_counter()
            cursor.insertHtml('<p style="margin:0"><span style="font-size:1pt">&#8203;</span></p>'+fragment)
            self.timings['max_insert_html_seconds']=max(self.timings.get('max_insert_html_seconds',0),time.perf_counter()-native_start)
            if index==0 and self.has_cover:
                # Physical cover alignment is resolved while only the cover
                # exists. Editing every heading after a 1000-target layout
                # invalidates the complete document and stalls the GUI's GIL.
                self.doc.setLayoutEnabled(False);current=self.doc.begin()
                while current.isValid():
                    it=current.begin();key=None
                    while not it.atEnd():
                        frag=it.fragment()
                        if frag.isValid():key=next((n for n in frag.charFormat().anchorNames() if n in self.items),None)
                        if key:break
                        it+=1
                    if key:
                        item=self.items[key];fmt=current.blockFormat();align=(item.get('style') or {}).get('align','right')
                        fmt.setAlignment({'left':Qt.AlignLeft,'center':Qt.AlignHCenter}.get(align,Qt.AlignRight)|Qt.AlignAbsolute)
                        c=QTextCursor(current);c.setBlockFormat(fmt)
                    current=current.next()
                self.doc.setLayoutEnabled(True)
            native_start=time.perf_counter();self.doc.documentLayout().documentSize()
            self.timings['max_incremental_size_seconds']=max(self.timings.get('max_incremental_size_seconds',0),time.perf_counter()-native_start)
            if progress is not None and not first_sent and self.doc.pageCount()>1:
                self.page_count=self.doc.pageCount();self.building=True
                image=QImage(int(WIDTH),int(HEIGHT),QImage.Format_RGB32);p=QPainter(image);self.draw_page(p,0);p.end();progress(image);first_sent=True
                self.timings['first_preview_seconds']=time.perf_counter()-started
            time.sleep(.0005)
        self.timings['incremental_html']=time.perf_counter()-tick;tick=time.perf_counter()
        self.doc.documentLayout().documentSize()
        self.timings['resources_and_layout']=time.perf_counter()-tick;tick=time.perf_counter()
        cursor=self.doc.begin()
        while cursor.isValid():
            checkpoint('layout_anchors')
            it=cursor.begin()
            while not it.atEnd():
                frag=it.fragment()
                if frag.isValid():
                    for name in frag.charFormat().anchorNames():
                        self.anchors.setdefault(name,self.doc.documentLayout().blockBoundingRect(cursor))
                it+=1
            cursor=cursor.next()
        self.page_count=max(1,self.doc.pageCount())
        self.building=False
        self.timings['anchors']=time.perf_counter()-tick;self.timings['total']=time.perf_counter()-started
    def hit(self,page,point):
        local=(point.x()-MARGIN,point.y()-MARGIN+page*BODY_H)
        candidates=[(abs(rect.top()-local[1]),key) for key,rect in self.anchors.items() if rect.top()<=local[1]+5]
        return min(candidates)[1] if candidates and 0<=local[0]<=BODY_W else None
    def freeze(self):
        """Rasterize in the owning thread; no live QTextDocument crosses threads.

        PNGs are bounded in memory; the GUI only decodes visible pages. Anchors
        still address editable model blocks, not painted pixels.
        """
        from PySide6.QtCore import QBuffer,QIODevice
        started=time.perf_counter();pages=[];total=0
        for page in range(self.page_count):
            checkpoint('preview_page')
            image=QImage(int(WIDTH),int(HEIGHT),QImage.Format_RGB32)
            painter=QPainter(image)
            try:self.draw_page(painter,page)
            finally:painter.end()
            buffer=QBuffer();buffer.open(QIODevice.WriteOnly)
            if not image.save(buffer,'PNG'):raise ValueError('تعذر إعداد صفحة المعاينة')
            payload=bytes(buffer.data());total+=len(payload)
            if total>256*1024*1024:raise ValueError('تجاوزت صفحات المعاينة ميزانية 256 MiB؛ قلل محتوى التقرير')
            pages.append(payload);time.sleep(.0005)
        self.timings['preview_raster_seconds']=time.perf_counter()-started
        self.timings['preview_compressed_bytes']=total
        return FrozenPreview(pages,self.anchors,self.items,self.timings)
    def draw_page(self,painter,page):
        painter.fillRect(QRectF(0,0,WIDTH,HEIGHT),Qt.white)
        if page==0 and self.has_cover:painter.fillRect(QRectF(0,0,WIDTH,HEIGHT),QColor('#183c57'))
        painter.save();painter.translate(MARGIN,MARGIN-page*BODY_H)
        painter.setClipRect(QRectF(0,page*BODY_H,BODY_W,BODY_H));self.doc.drawContents(painter,QRectF(0,page*BODY_H,BODY_W,BODY_H));painter.restore()
        painter.setPen(Qt.white if page==0 and self.has_cover else Qt.darkGray);painter.setFont(QFont('Arial',9))
        painter.drawText(QRectF(MARGIN,HEIGHT-45,BODY_W-70,24),Qt.AlignRight|Qt.AlignAbsolute,'ChangeVision 2026')
        painter.drawText(QRectF(MARGIN,HEIGHT-45,70,24),Qt.AlignLeft|Qt.AlignAbsolute|Qt.TextForceLeftToRight,f'{page+1} / '+('…' if getattr(self,'building',False) else str(self.page_count)))

class FrozenPreview:
    """Thread-independent page bytes, stable hit map and four-page decode cache."""
    hit=ReportLayout.hit
    def __init__(self,pages,anchors,items,timings):
        self.pages=tuple(pages);self.page_count=len(pages);self.anchors=dict(anchors);self.items=items;self.timings=dict(timings);self.cache=OrderedDict()
    def draw_page(self,painter,page):
        if page not in self.cache:
            self.cache[page]=QImage.fromData(self.pages[page])
            if len(self.cache)>4:self.cache.popitem(last=False)
        self.cache.move_to_end(page);painter.drawImage(QRectF(0,0,WIDTH,HEIGHT),self.cache[page])

@cancellable
def export_pdf(path,document,assets,overwrite=False):
    path=validate_output_path(path,create_parent=False).resolve()
    if path.exists() and not overwrite:raise FileExistsError(str(path))
    if any(a.get('source_ref') and Path(a['source_ref']).resolve()==path for a in document.to_dict()['assets'].values()):raise ValueError('لا تستبدل الصورة الأصلية')
    layout=ReportLayout(document,assets);fd,temp=tempfile.mkstemp(dir=path.parent,suffix='.pdf');os.close(fd)
    writer=None;p=None
    try:
        writer=QPdfWriter(temp);writer.setResolution(96);writer.setPageSize(QPageSize(QPageSize.A4))
        from PySide6.QtCore import QMarginsF
        writer.setPageMargins(QMarginsF(0,0,0,0));writer.setTitle(document.to_dict()['title']);p=QPainter(writer)
        for page in range(layout.page_count):
            checkpoint('pdf_page')
            if page:writer.newPage()
            p.save()
            try:p.scale(writer.width()/WIDTH,writer.height()/HEIGHT);layout.draw_page(p,page)
            finally:p.restore()
        p.end();p=None;writer=None;checkpoint('pdf_validate')
        if not Path(temp).read_bytes().startswith(b'%PDF-'):raise ValueError('فشل إنشاء PDF')
        checkpoint('pdf_before_commit');commit_file(temp,path)
    finally:
        if p is not None and p.isActive():p.end()
        p=None;writer=None
        if os.path.exists(temp):os.unlink(temp)
