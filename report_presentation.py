"""Presentation rules only; never changes report records or image identities."""
import re

NAVY='183C57'
ACCENT='3BA7AD'
BODY_SIZE=12
HEADING_SIZE=19

def runs(value):
    """Keep contiguous Latin identifiers/numbers independent of Arabic runs."""
    return [p for p in re.split(r'([+\-]?[A-Za-z0-9][A-Za-z0-9.,/:_+%eE\-]*(?:[ \t]+[+\-]?[A-Za-z0-9][A-Za-z0-9.,/:_+%eE\-]*)*)',str(value)) if p]

def groups(blocks):
    """Consecutive fields become compact editable cards, retaining all blocks."""
    fields=[];blocks=list(blocks);index=0
    while index<len(blocks):
        b=blocks[index];index+=1
        if b['kind']=='heading' and b.get('target_uid') and index<len(blocks) and blocks[index]['kind']=='field' and blocks[index].get('target_uid')==b['target_uid']:
            if fields:yield 'fields',fields;fields=[]
            card=[b]
            while index<len(blocks) and blocks[index]['kind']=='field' and blocks[index].get('target_uid')==b['target_uid']:card.append(blocks[index]);index+=1
            yield 'target_card',card;continue
        if b['kind']=='field':fields.append(b);continue
        if fields:yield 'fields',fields;fields=[]
        yield b['kind'],[b]
    if fields:yield 'fields',fields

def cover_counts(data):
    from report_document import item_included
    selected=data.get('target_selection',{})
    targets=sum(selected.get(t['uid'],True) for t in data['targets'])
    images=sum(b['kind']=='image' and item_included(data,b) for s in data['sections'] if s['visible'] for b in s['blocks'])
    return [('الأهداف المدرجة',targets),('مواضع صور التقرير',images)]

def column_weight(column):
    weight=max(.1,float(column.get('width',1)))
    if weight!=1:return weight
    return {'id':.5,'target_id':.5,'target_name':1.5,'longitude':2,'latitude':2}.get(column['key'],weight)
