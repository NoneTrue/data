import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,unittest,zipfile,io,copy
from pathlib import Path
from lxml import etree as E
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage,QPainter
from report_adapter import from_snapshot
from report_docx import export_docx
from report_layout import ReportLayout,WIDTH,HEIGHT
from report_presentation import runs

APP=QApplication.instance() or QApplication([])
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main','v':'urn:schemas-microsoft-com:vml'}
class PresentationTests(unittest.TestCase):
    def test_complex_script_metrics_and_native_cover_fields(self):
        document,assets=from_snapshot({'report_metadata':{'operation_name':'عملية اختبار Test 2026'},'regions':[{'id':7,'target_uid':'T7','report_included':True,'longitude':'-49.1234567890123','latitude':'11.280549690857843'}]})
        before=document.to_dict()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.docx';export_docx(path,document,assets)
            with zipfile.ZipFile(path) as z:
                tree=E.fromstring(z.read('word/document.xml'))
                arabic=[r for r in tree.xpath('//w:r',namespaces=NS) if any('\u0600'<=c<='\u06ff' for c in ''.join(r.itertext()))]
                self.assertTrue(arabic)
                for r in arabic:self.assertEqual(r.find('w:rPr/w:sz',NS).get('{'+NS['w']+'}val'),r.find('w:rPr/w:szCs',NS).get('{'+NS['w']+'}val'))
                alltext=''.join(tree.itertext());self.assertIn('-49.1234567890123',alltext);self.assertIn('11.280549690857843',alltext)
                self.assertGreater(len(tree.xpath('//w:tbl',namespaces=NS)),1)
                headers=[E.fromstring(z.read(n)) for n in z.namelist() if n.startswith('word/header') and n.endswith('.xml')]
                self.assertEqual(sum(len(h.xpath('//v:rect[@fillcolor="#183C57"]',namespaces=NS)) for h in headers),1)
        self.assertEqual(before,document.to_dict())
    def test_frozen_preview_has_no_qtextdocument_and_bounded_decode_cache(self):
        document,assets=from_snapshot({'regions':[{'id':i,'target_uid':str(i),'report_included':True} for i in range(20)]})
        layout=ReportLayout(document,assets);frozen=layout.freeze()
        self.assertFalse(hasattr(frozen,'doc'));self.assertEqual(frozen.anchors,layout.anchors)
        image=QImage(int(WIDTH),int(HEIGHT),QImage.Format_RGB32);p=QPainter(image)
        try:
            for page in range(frozen.page_count):frozen.draw_page(p,page)
        finally:p.end()
        self.assertLessEqual(len(frozen.cache),4)
    def test_caption_chain_for_both_variants(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'source.png';Image.new('RGB',(40,20),'blue').save(source)
            document,assets=from_snapshot({'regions':[{'id':7,'target_uid':'T7','report_included':True,'report_images':[{'path':str(source),'source':'after'}]}]})
            data=document.to_dict()
            image=next(b for s in data['sections'] for b in s['blocks'] if b['kind']=='image');image['variant']='both'
            from report_document import ReportDocument
            document=ReportDocument(data);path=Path(temp)/'out.docx';export_docx(path,document,assets)
            with zipfile.ZipFile(path) as z:tree=E.fromstring(z.read('word/document.xml'))
            pictures=tree.xpath('//w:p[w:r/w:drawing]',namespaces=NS);self.assertEqual(len(pictures),2)
            for picture in pictures:
                self.assertIsNotNone(picture.getprevious().find('w:pPr/w:keepNext',NS))
                self.assertIsNotNone(picture.find('w:pPr/w:keepLines',NS))
    def test_mixed_runs_preserve_exact_text_and_scientific_values(self):
        value='هدف ID 196 — EPSG:4326 سالب -49.1234567890123 قيمة 1.2700189202519615e-06'
        result=runs(value);self.assertEqual(''.join(result),value)
        self.assertTrue(any('1.2700189202519615e-06' in part for part in result))
        self.assertTrue(any('-49.1234567890123' in part for part in result))

    def test_qt_heading_uses_actual_point_size_without_relative_override(self):
        from PySide6.QtGui import QTextFormat
        document,assets=from_snapshot({'regions':[]})
        layout=ReportLayout(document,assets)
        key=document.to_dict()['sections'][0]['blocks'][0]['uid']
        rect=layout.anchors[key];block=layout.doc.begin();found=False
        while block.isValid():
            it=block.begin()
            while not it.atEnd():
                fragment=it.fragment()
                if fragment.isValid() and key in fragment.charFormat().anchorNames():
                    self.assertAlmostEqual(fragment.charFormat().font().pointSizeF(),32)
                    self.assertFalse(fragment.charFormat().hasProperty(QTextFormat.FontSizeAdjustment));found=True
                it+=1
            block=block.next()
        self.assertTrue(found)

if __name__=='__main__':unittest.main()
