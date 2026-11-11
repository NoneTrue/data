"""python-docx backend consumes the same fixed report revision as PDF."""
from __future__ import annotations
import io
import os
from pathlib import Path
import tempfile
import zipfile
import re
from docx import Document
from docx.shared import Mm,Pt,RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_DIRECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml import parse_xml
from report_layout import display,latin,image_payload,table_bands
from report_document import item_included,rows_for_report
from report_presentation import runs,groups,cover_counts,column_weight,BODY_SIZE,HEADING_SIZE
from report_cancel import checkpoint,commit_file,cancellable,CheckedWriter
from storage_manager import validate_output_path

def text(par,value,style=None):
    style=style or {};value=display(value);ltr=latin(value)
    par.alignment={'left':WD_ALIGN_PARAGRAPH.LEFT,'center':WD_ALIGN_PARAGRAPH.CENTER}.get(style.get('align'),WD_ALIGN_PARAGRAPH.LEFT if ltr else WD_ALIGN_PARAGRAPH.RIGHT)
    bidi=OxmlElement('w:bidi');bidi.set(qn('w:val'),'0' if ltr else '1');par._p.get_or_add_pPr().append(bidi)
    par.paragraph_format.space_after=Pt(6);par.paragraph_format.line_spacing=1.12
    parts=runs(value)
    for part in parts:
        checkpoint('docx_text')
        if not part:continue
        size=max(7,min(48,float(style.get('size',BODY_SIZE))))
        r=par.add_run(part);r.font.name=style.get('font','Arial');r.font.size=Pt(size)
        r.bold=bool(style.get('bold'));r.italic=bool(style.get('italic'));r.font.rtl=not latin(part)
        color=style.get('color','#183c57').lstrip('#');r.font.color.rgb=RGBColor.from_string(color)
        r._element.get_or_add_rPr().rFonts.set(qn('w:cs'),style.get('font','Arial'))
        # Arabic complex-script metrics are separate from Latin metrics in Word.
        for tag,val in [('szCs',str(int(size*2))),('bCs','1' if r.bold else '0'),('iCs','1' if r.italic else '0')]:
            element=OxmlElement('w:'+tag);element.set(qn('w:val'),val);r._element.get_or_add_rPr().append(element)
    return par.runs[-1] if par.runs else par.add_run('')

def shade(cell,color):
    sh=OxmlElement('w:shd');sh.set(qn('w:fill'),color);cell._tc.get_or_add_tcPr().append(sh)

def field_card(doc,items):
    table=doc.add_table(rows=0,cols=4);table.autofit=False;table.table_direction=WD_TABLE_DIRECTION.RTL
    for column,width in zip(table.columns,[Mm(28),Mm(60),Mm(28),Mm(60)]):column.width=width
    for index in range(0,len(items),2):
        checkpoint('docx_fields')
        row=table.add_row();row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for offset,b in enumerate(items[index:index+2]):
            label,value=row.cells[offset*2:offset*2+2];shade(value,'FFFFFF')
            text(label.paragraphs[0],b.get('label',''),{'size':10,'bold':True});shade(label,'E8F0F5')
            p=value.paragraphs[0];text(p,display(b.get('value'))+(' '+b['unit'] if b.get('unit') else ''),{'size':11,**(b.get('style') or {})})
            if not str(b.get('source','')).startswith('target:'):
                text(value.add_paragraph(),'المصدر: '+b.get('source','مستخدم'),{'size':8,'color':'536B7A'})
        for cell in row.cells:
            for p in cell.paragraphs:p.paragraph_format.space_after=Pt(4)
    sources=list(dict.fromkeys(b.get('source','') for b in items if str(b.get('source','')).startswith('target:')))
    if sources:text(doc.add_paragraph(),'مرجع بيانات الهدف: '+'، '.join(sources),{'size':8,'color':'536B7A'})

def cover_background(sec):
    sec.different_first_page_header_footer=True
    # Native Word background shape in the first-page header; body text stays editable.
    shape=parse_xml('''<w:pict xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"
        xmlns:w10="urn:schemas-microsoft-com:office:word">
        <v:rect id="ChangeVisionCover" o:allowincell="f" fillcolor="#183C57" stroked="f"
        style="position:absolute;left:0;top:0;width:595.3pt;height:841.9pt;z-index:-251654144;mso-position-horizontal-relative:page;mso-position-vertical-relative:page">
        <w10:wrap type="none"/></v:rect></w:pict>''')
    sec.first_page_header.paragraphs[0].add_run()._r.append(shape)
    text(sec.first_page_footer.paragraphs[0],'ChangeVision 2026',{'size':9,'color':'FFFFFF'})
    fld=OxmlElement('w:fldSimple');fld.set(qn('w:instr'),'PAGE')
    run=OxmlElement('w:r');pr=OxmlElement('w:rPr');color=OxmlElement('w:color');color.set(qn('w:val'),'FFFFFF');pr.append(color);run.append(pr)
    cached=OxmlElement('w:t');cached.text='1';run.append(cached);fld.append(run);sec.first_page_footer.paragraphs[0]._p.append(fld)

@cancellable
def export_docx(path,document,assets,overwrite=False):
    data=document.to_dict();path=validate_output_path(path,create_parent=False).resolve()
    if path.exists() and not overwrite:raise FileExistsError(str(path))
    if any(a.get('source_ref') and Path(a['source_ref']).resolve()==path for a in data['assets'].values()):raise ValueError('لا تستبدل الصورة الأصلية')
    doc=Document();sec=doc.sections[0];sec.page_width=Mm(210);sec.page_height=Mm(297)
    sec.left_margin=sec.right_margin=Mm(17);sec.top_margin=sec.bottom_margin=Mm(18)
    text(sec.header.paragraphs[0],data['title'],{'size':9})
    p=sec.footer.paragraphs[0];text(p,'ChangeVision 2026 | صفحة ',{'size':9});fld=OxmlElement('w:fldSimple');fld.set(qn('w:instr'),'PAGE');p._p.append(fld)
    first=next((s for s in data['sections'] if s['visible']),None)
    has_cover=bool(first and first['kind']=='cover')
    if has_cover:cover_background(sec)
    for section in data['sections']:
        checkpoint('docx_section')
        if not section['visible']:continue
        dark=has_cover and section is first
        if dark:
            p=doc.add_paragraph();text(p,'CHANGEVISION 2026',{'size':15,'bold':True,'color':'FFFFFF','align':'left'})
            p.paragraph_format.space_after=Pt(50)
        p=doc.add_paragraph();p.paragraph_format.keep_with_next=True
        text(p,section['title'],{'size':11 if dark else HEADING_SIZE,'bold':True,'color':'BBDDE8' if dark else '183C57'})
        p.paragraph_format.space_before=Pt(12);p.paragraph_format.space_after=Pt(12)
        for kind,items in groups(b for b in section['blocks'] if item_included(data,b)):
            checkpoint('docx_block')
            if kind=='target_card':
                p=doc.add_paragraph();p.paragraph_format.keep_with_next=True;p.paragraph_format.space_before=Pt(12)
                text(p,items[0].get('text',''),{'size':16,'bold':True,**(items[0].get('style') or {})})
                field_card(doc,items[1:]);continue
            if kind=='fields':field_card(doc,items);continue
            b=items[0]
            kind=b['kind'];style=b.get('style') or {}
            if kind in ['heading','paragraph','note','conclusion']:
                p=doc.add_paragraph();p.paragraph_format.keep_with_next=kind=='heading'
                defaults={'size':32 if dark else 16,'bold':True} if kind=='heading' else {'size':BODY_SIZE}
                if dark:defaults.update(color='FFFFFF')
                text(p,b.get('text',''),{**defaults,**style})
                if kind=='heading':p.paragraph_format.space_before=Pt(16);p.paragraph_format.space_after=Pt(8)
            elif kind=='field':
                text(doc.add_paragraph(),b.get('label','')+': '+display(b.get('value'))+' '+b.get('unit',''),style)
                text(doc.add_paragraph(),'المصدر: '+b.get('source','مستخدم'),{'size':8})
            elif kind=='table':
                for cols in table_bands(b):
                    table=doc.add_table(rows=1,cols=len(cols));table.style='Table Grid';table.autofit=False;table.table_direction=WD_TABLE_DIRECTION.RTL
                    total=sum(column_weight(c) for _,c in cols)
                    widths=[Mm(176*column_weight(c)/total) for _,c in cols]
                    for column,width in zip(table.columns,widths):column.width=width
                    for ri,values in enumerate([[c.get('title',c['key']) for _,c in cols]]+[[display(row[i]) for i,c in cols] for row in rows_for_report(data,b)]):
                        checkpoint('docx_table_row')
                        row=table.rows[0] if ri==0 else table.add_row();cells=row.cells
                        pr=row._tr.get_or_add_trPr();pr.append(OxmlElement('w:cantSplit'))
                        if ri==0:pr.append(OxmlElement('w:tblHeader'))
                        for ci,(cell,value) in enumerate(zip(cells,values)):
                            cell.width=widths[ci];r=text(cell.paragraphs[0],value,{'size':11,'bold':ri==0,'color':'#ffffff' if ri==0 else '#183c57'})
                            if ri%2==0 and ri>0:shade(cell,'F1F6F9')
                            if ri==0:
                                sh=OxmlElement('w:shd');sh.set(qn('w:fill'),'183C57');cell._tc.get_or_add_tcPr().append(sh)
            elif kind=='image':
                variants=['original','annotated'] if b.get('variant')=='both' else [b.get('variant','original')]
                for variant in variants:
                    checkpoint('docx_image')
                    # Each variant has its own heading chain; do not orphan the second image.
                    p=doc.add_paragraph();p.paragraph_format.keep_with_next=True;text(p,b.get('caption',''),{'size':14,'bold':True})
                    p.paragraph_format.space_before=Pt(14)
                    payload=image_payload({**b,'variant':variant},assets)
                    if payload:
                        from PIL import Image
                        with Image.open(io.BytesIO(payload)) as im:
                            width=min(176,max(10,float(b.get('width_mm',140))));height=width*im.height/max(1,im.width)
                            if height>210:width*=210/height
                        picture=doc.add_paragraph();picture.alignment=WD_ALIGN_PARAGRAPH.CENTER
                        picture.paragraph_format.keep_together=True;picture.paragraph_format.space_after=Pt(10)
                        picture.add_run().add_picture(io.BytesIO(payload),width=Mm(width))
                    else:text(doc.add_paragraph(),'الصورة غير متوفرة — '+str(b.get('source_ref') or ''))
                if b.get('description'):text(doc.add_paragraph(),b['description'])
        if dark:
            counts=doc.add_table(rows=1,cols=2);counts.table_direction=WD_TABLE_DIRECTION.RTL
            for cell,(label,count) in zip(counts.rows[0].cells,cover_counts(data)):
                shade(cell,'294F68');text(cell.paragraphs[0],str(count),{'size':27,'bold':True,'color':'FFFFFF','align':'center'})
                text(cell.add_paragraph(),label,{'size':12,'color':'FFFFFF','align':'center'})
            p=doc.add_paragraph();text(p,'تقرير تحليل التغيرات • الأهداف والصور والبيانات المرجعية',{'size':13,'color':'BBDDE8'})
        if section['kind']=='cover':doc.add_page_break()
    fd,temp=tempfile.mkstemp(dir=path.parent,suffix='.docx');os.close(fd)
    try:
        checkpoint('docx_package')
        with open(temp,'wb') as stream:doc.save(CheckedWriter(stream))
        checkpoint('docx_package_closed')
        with zipfile.ZipFile(temp) as z:
            for name in z.namelist():
                with z.open(name) as member:
                    while member.read(65536):checkpoint('docx_verify')
        checkpoint('docx_reopen');Document(temp);checkpoint('docx_before_commit');commit_file(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
