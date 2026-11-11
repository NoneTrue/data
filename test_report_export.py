import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image
from PySide6.QtWidgets import QApplication

from report_export import SECTIONS, export_docx, export_pdf, report_html


APP = QApplication.instance() or QApplication([])


class ReportExportTests(unittest.TestCase):
    def test_explicit_target_identity_metadata_and_sources_are_rendered(self):
        snapshot = {
            'report_metadata': {'operation_name': 'مهمة مراقبة', 'longitude': 46.7,
                                'latitude': 24.7, '_sources': {'longitude': 'EXIF GPS'}},
            'region_columns': [dict(c) for c in __import__('target_management').DEFAULT_COLUMNS],
            'regions': [{'id': 15, 'target_uid': 'stable-15', 'target_name': 'طائرة',
                         'target_state': 'مهم', 'longitude': 46.71, 'latitude': 24.71,
                         'description': 'وصف', 'notes': 'ملاحظة', 'report_included': True},
                        {'id': 20, 'target_name': 'مبنى', 'report_included': False}],
            'objects': [], 'change_summary': {},
        }
        html = report_html(snapshot, ['overview', 'targets'])
        self.assertIn('اسم الهدف أو المعلم المحدد', html)
        self.assertIn('طائرة', html)
        self.assertIn('15', html)
        self.assertIn('46.71', html)
        self.assertIn('EXIF GPS', html)
        self.assertNotIn('مبنى', html)

    def test_pdf_and_docx_include_rtl_text_and_target_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / 'target.png'
            Image.new('RGB', (32, 24), 'orange').save(image_path)
            snapshot = {
                'image1': {'name': 'قبل', 'path': 'before.tif', 'width': 32, 'height': 24},
                'image2': {'name': 'بعد', 'path': 'after.tif', 'width': 32, 'height': 24},
                'regions': [{'title': 'هدف اختباري', 'report_included': True,
                             'image_layers': {'before': {'composite_path': str(image_path)}}}],
                'objects': [{'object_id': 4, 'change_type': 'ظهور محتمل'}],
                'change_summary': {'changed_percent': 3.5},
            }
            html = report_html(snapshot, [key for key, _ in SECTIONS])
            self.assertIn('هدف اختباري', html)
            docx = root / 'report.docx'; pdf = root / 'report.pdf'
            export_docx(docx, html); export_pdf(pdf, html)
            with zipfile.ZipFile(docx) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn('word/document.xml', archive.namelist())
                self.assertTrue(any(name.startswith('word/media/') for name in archive.namelist()))
                document_xml = archive.read('word/document.xml')
                ET.fromstring(document_xml)
                ET.fromstring(archive.read('[Content_Types].xml'))
                ET.fromstring(archive.read('word/_rels/document.xml.rels'))
                self.assertIn('هدف اختباري', document_xml.decode('utf-8'))
            self.assertTrue(pdf.read_bytes().startswith(b'%PDF-'))

    def test_excluded_targets_are_omitted_and_section_order_is_respected(self):
        snapshot = {'regions': [{'title': 'خاص', 'report_included': False},
                                {'title': 'عام', 'report_included': True}],
                    'objects': [], 'change_summary': {}}
        html = report_html(snapshot, ['targets', 'overview'])
        self.assertNotIn('خاص', html)
        self.assertIn('عام', html)
        self.assertLess(html.index('الأهداف'), html.index('ملخص المشروع'))


if __name__ == '__main__':
    unittest.main()
