import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from PySide6.QtCore import Qt,QPointF
from PySide6.QtGui import QImage,QPainter,QFontDatabase
from PySide6.QtWidgets import QApplication,QInputDialog
from PySide6.QtTest import QTest
from image_editor import ImageEditorDialog,draw_report_annotation
from report_adapter import from_snapshot
from report_project import save_project,load_project
from report_layout import image_payload

APP=QApplication.instance() or QApplication([])
for name in ['arial.ttf','arialbd.ttf']:QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name))

class ReportAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.rgb=np.full((240,320,3),220,np.uint8);self.dialog=ImageEditorDialog(None,report_mode=True,image_rgb=self.rgb)
        self.dialog.show();APP.processEvents();self.addCleanup(self.close)
    def close(self):self.dialog.reject();self.dialog.deleteLater();APP.processEvents()
    def draw(self,kind,first,last):
        c=self.dialog.canvas;c.tool=kind
        p1=c.mapFromScene(QPointF(*first));p2=c.mapFromScene(QPointF(*last))
        QTest.mousePress(c.viewport(),Qt.LeftButton,Qt.NoModifier,p1);QTest.mouseMove(c.viewport(),p2,20);QTest.mouseRelease(c.viewport(),Qt.LeftButton,Qt.NoModifier,p2);APP.processEvents()
    def test_real_canvas_gesture_persists_circle_and_ellipse_separately(self):
        self.draw('circle',(30,30),(90,75));self.draw('ellipse',(150,80),(250,130))
        self.assertEqual(len(self.dialog.annotations),2)
        circle,ellipse=self.dialog.annotations
        self.assertEqual(abs(circle['points'][1][0]-circle['points'][0][0]),abs(circle['points'][1][1]-circle['points'][0][1]))
        self.assertNotEqual(abs(ellipse['points'][1][0]-ellipse['points'][0][0]),abs(ellipse['points'][1][1]-ellipse['points'][0][1]))
    def test_properties_independent_edit_text_position_undo_redo(self):
        a={'id':'text','type':'text','report_schema':1,'points':[(20,40)],'text':'قبل','font_size':20,'color':'#ff0000','size':2}
        b={'id':'shape','type':'rectangle','report_schema':1,'points':[(80,80),(130,130)],'color':'#00ff00','size':4}
        self.dialog.annotations=[copy.deepcopy(a),copy.deepcopy(b)];self.dialog.refresh_canvas();self.dialog.select_annotation(0)
        self.dialog.report_font_size.setValue(30);self.dialog.report_x.setValue(35);self.dialog.report_y.setValue(55);self.dialog.color='#123456'
        with patch.object(QInputDialog,'getText',return_value=('نص عربي معدل',True)):self.dialog.edit_annotation()
        self.assertEqual(self.dialog.annotations[1],b);self.assertEqual(self.dialog.annotations[0]['text'],'نص عربي معدل');self.assertEqual(self.dialog.annotations[0]['font_size'],30)
        self.assertEqual(self.dialog.annotations[0]['points'][0],(35.,55.));after=copy.deepcopy(self.dialog.annotations)
        self.dialog.undo();self.assertEqual(self.dialog.annotations,[a,b]);self.dialog.redo();self.assertEqual(self.dialog.annotations,after)
    def test_duplicate_z_order_crop_rotation_and_composite(self):
        self.draw('rectangle',(50,60),(100,120));self.dialog.select_annotation(0);self.dialog._report_duplicate()
        self.assertEqual(len(self.dialog.annotations),2);self.assertNotEqual(self.dialog.annotations[0]['id'],self.dialog.annotations[1]['id'])
        before=self.dialog.annotations[0]['id'];self.dialog.selected_annotation=0;self.dialog._report_order(1);self.assertEqual(self.dialog.annotations[1]['id'],before)
        self.dialog.apply_crop((10,10,220,180));self.dialog.rotate(True)
        self.assertEqual(self.dialog.base.shape[:2],(220,180));self.assertFalse(np.array_equal(self.dialog._composite(),self.dialog.base))
        self.assertTrue(np.array_equal(self.rgb,np.full((240,320,3),220,np.uint8)))
    def test_layers_survive_project_and_can_be_edited_again(self):
        self.draw('arrow',(20,30),(150,100));self.dialog.select_annotation(0);self.dialog.report_line.setCurrentText('dash');self.dialog.report_arrow.setValue(22);self.dialog.report_opacity.setValue(30);self.dialog._apply_report_properties()
        self.dialog.save_layers();result=self.dialog.result
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);p=root/'source.png';Image.fromarray(self.rgb).save(p);original=p.read_bytes()
            d,a=from_snapshot({'regions':[{'id':7,'target_uid':'uid-7','report_included':True,'report_images':[{'path':str(p),'source':'after'}]}]})
            data=d.to_dict();item=next(b for s in data['sections'] for b in s['blocks'] if b['kind']=='image');item.update(annotations=result['annotations'],variant='annotated')
            from report_document import ReportDocument
            d=ReportDocument(data);path=root/'layers.cvreport';save_project(path,d,a);restored,store=load_project(path)
            item2=next(b for s in restored.to_dict()['sections'] for b in s['blocks'] if b['kind']=='image');self.assertEqual(item2['annotations'],__import__('json').loads(__import__('json').dumps(result['annotations'])))
            with Image.open(io.BytesIO(store.payloads[item2['asset_uid']])) as im:rgb=np.asarray(im.convert('RGB')).copy()
            again=ImageEditorDialog(None,{'annotations':item2['annotations']},report_mode=True,image_rgb=rgb)
            try:
                again.select_annotation(0);again.size.setValue(9);again._apply_report_properties();self.assertEqual(again.annotations[0]['size'],9)
            finally:again.reject()
            self.assertNotEqual(image_payload(item2,store),store.payloads[item2['asset_uid']]);self.assertEqual(p.read_bytes(),original)
    def test_all_new_shapes_render_without_mutating_properties(self):
        image=QImage(320,240,QImage.Format_ARGB32);image.fill(Qt.white);p=QPainter(image)
        for kind in ['arrow','line','rectangle','circle','ellipse','freehand','polygon','text']:
            a={'id':kind,'type':kind,'points':[(20,40),(120,40),(80,130)] if kind in ['polygon','freehand'] else [(20,40),(120,130)],'text':'معلم عربي','font_family':'Arial','font_size':24,'size':4,'color':'#123456','rotation':20,'fill_opacity':.4,'line_style':'dash','arrow_size':22,'text_background':'#ffffff','text_background_opacity':.6}
            before=copy.deepcopy(a);draw_report_annotation(p,a);self.assertEqual(a,before)
        p.end();self.assertFalse(image.isNull())
    def test_drag_resize_number_and_polygon_gestures(self):
        self.draw('rectangle',(30,30),(80,80));c=self.dialog.canvas;c.tool='select'
        self.draw('select',(45,45),(65,65));self.assertAlmostEqual(min(p[0] for p in self.dialog.annotations[0]['points']),50,delta=2)
        points=self.dialog.annotations[0]['points'];bottom=(max(p[0] for p in points),max(p[1] for p in points));self.draw('select',bottom,(bottom[0]+30,bottom[1]+20))
        self.assertGreater(abs(self.dialog.annotations[0]['points'][1][0]-self.dialog.annotations[0]['points'][0][0]),70)
        c.tool='number';QTest.mouseClick(c.viewport(),Qt.LeftButton,Qt.NoModifier,c.mapFromScene(QPointF(200,50)))
        self.assertTrue(self.dialog.annotations[-1]['numbered'])
        c.tool='polygon'
        for pt in [(160,130),(230,130),(200,200)]:QTest.mouseClick(c.viewport(),Qt.LeftButton,Qt.NoModifier,c.mapFromScene(QPointF(*pt)))
        QTest.mouseDClick(c.viewport(),Qt.LeftButton,Qt.NoModifier,c.mapFromScene(QPointF(200,200)))
        self.assertEqual(self.dialog.annotations[-1]['type'],'polygon');self.assertGreaterEqual(len(self.dialog.annotations[-1]['points']),3)
    def test_existing_layer_composite_and_separate_original_working_are_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);original=root/'original.png';working=root/'working.png';composite=root/'composite.png'
            Image.new('RGB',(100,80),'orange').save(original);Image.new('RGB',(40,30),'blue').save(working);Image.new('RGB',(40,30),'green').save(composite)
            state={'original_path':str(original),'working_path':str(working),'composite_path':str(composite),'brightness':10,'contrast':120,'sharpness':15,'annotations':[{'id':'legacy','type':'circle','points':[[2,3],[20,15]],'color':'#ff0000','size':2}]}
            doc,assets=from_snapshot({'regions':[{'id':1,'target_uid':'layer-target','report_included':True,'image_layers':{'before':state}}]})
            item=next(b for s in doc.to_dict()['sections'] for b in s['blocks'] if b['kind']=='image')
            self.assertEqual(item['variant'],'annotated');self.assertEqual(item['annotations'][0]['type'],'ellipse')
            with Image.open(io.BytesIO(image_payload(item,assets))) as im:self.assertEqual(im.getpixel((0,0)),(0,128,0))
            with Image.open(io.BytesIO(image_payload({**item,'variant':'original'},assets))) as im:self.assertEqual(im.size,(100,80));self.assertEqual(im.getpixel((0,0)),(255,165,0))
            again=ImageEditorDialog(None,item['layer_adjustments'],report_mode=True,image_rgb=np.full((30,40,3),80,np.uint8),original_rgb=np.full((80,100,3),120,np.uint8))
            try:self.assertEqual(again.original.shape[:2],(80,100));self.assertEqual(again.base.shape[:2],(30,40));self.assertNotEqual(int(again._adjusted()[0,0,0]),80)
            finally:again.reject()
    def test_flat_composite_does_not_invent_missing_original_or_double_annotations(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'composite.png';Image.new('RGB',(30,20),'blue').save(p)
            doc,assets=from_snapshot({'regions':[{'id':1,'target_uid':'missing-original','report_included':True,'image_layers':{'after':{'composite_path':str(p),'annotations':[{'id':'flattened','type':'text','points':[[2,10]],'text':'لا تضاعف'}]}}}]})
            item=next(b for s in doc.to_dict()['sections'] for b in s['blocks'] if b['kind']=='image')
            self.assertFalse(item['original_available']);self.assertEqual(item['annotations'],[]);self.assertIsNone(image_payload({**item,'variant':'original'},assets));self.assertTrue(doc.to_dict()['warnings'])

if __name__=='__main__':unittest.main()
