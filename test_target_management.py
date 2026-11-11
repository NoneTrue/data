import unittest
import tempfile
import zipfile

from report_manager import ReportManager
from target_management import (
    DEFAULT_COLUMNS, ensure_target_metadata, set_column_value,
    value_for_column, visible_columns,
)


class TargetManagementTests(unittest.TestCase):
    def test_default_report_columns_match_requested_order(self):
        self.assertEqual(
            [column['title'] for column in DEFAULT_COLUMNS],
            ['انتقال', 'ID', 'اسم العملية', 'اسم الهدف أو المعلم المحدد', 'العنوان', 'تاريخ بدء العملية',
             'تاريخ انتهاء العملية', 'تاريخ الصورة قبل', 'تاريخ الصورة بعد',
             'الموقع', 'Longitude', 'Latitude', 'الملاحظات'],
        )

    def test_target_name_is_user_entered_and_bound_to_stable_target(self):
        targets = [{'id': 15}, {'id': 20}]
        ensure_target_metadata(targets)
        self.assertEqual([target['target_name'] for target in targets], ['', ''])
        uid = targets[0]['target_uid']
        set_column_value(targets[0], DEFAULT_COLUMNS[3], 'طائرة')
        targets.reverse()
        self.assertEqual(targets[1]['target_name'], 'طائرة')
        self.assertEqual(targets[1]['target_uid'], uid)

    def test_old_saved_columns_are_migrated_without_changing_known_order(self):
        old = [dict(column) for column in DEFAULT_COLUMNS if column['key'] != 'target_name']
        migrated = visible_columns(old)
        self.assertEqual(migrated[3]['key'], 'target_name')

    def test_target_defaults_preserve_analytical_fields(self):
        regions = [{'id': 7, 'area_pixels': 19, 'classification': 'تغير مرجح'}]
        ensure_target_metadata(regions)
        target = regions[0]
        self.assertEqual(target['area_pixels'], 19)
        self.assertEqual(target['classification'], 'تغير مرجح')
        self.assertFalse(target['report_included'])
        self.assertTrue(target['target_uid'])
        self.assertEqual(target['target_state'], 'تغير مرجح')

    def test_report_exclusion_is_independent_of_analysis_data(self):
        target = {'id': 2, 'area_pixels': 31, 'report_included': False}
        ensure_target_metadata([target])
        self.assertEqual(target['area_pixels'], 31)
        self.assertFalse(target['report_included'])

    def test_column_titles_are_separate_from_stable_keys(self):
        target = {'id': 12, 'operation_name': 'مسح', 'excel_values': {}}
        column = {'key': 'operation_name', 'title': 'عنوان قابل للتغيير'}
        set_column_value(target, column, 'تحليل جديد')
        self.assertEqual(value_for_column(target, column), 'تحليل جديد')
        column['title'] = 'اسم آخر'
        self.assertEqual(column['key'], 'operation_name')
        self.assertEqual(value_for_column(target, column), 'تحليل جديد')

    def test_custom_column_order_and_visibility_are_retained(self):
        custom = [dict(c) for c in DEFAULT_COLUMNS]
        custom[2], custom[3] = custom[3], custom[2]
        custom[2]['visible'] = False
        normalized = visible_columns(custom)
        self.assertEqual(normalized[2]['key'], custom[2]['key'])
        self.assertFalse(normalized[2]['visible'])

    def test_report_manager_exports_new_default_region_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            reporter = ReportManager(folder, 'اختبار')
            reporter._write_excel({
                'regions': [
                    {'id': 1, 'target_name': 'مدرج', 'report_included': True},
                    {'id': 2, 'target_name': 'مستبعد', 'report_included': False},
                ],
            })
            with zipfile.ZipFile(reporter.xlsx_path) as workbook:
                sheet = workbook.read('xl/worksheets/sheet7.xml').decode('utf-8')
            self.assertIn('تاريخ بدء العملية', sheet)
            self.assertIn('الملاحظات', sheet)
            self.assertNotIn('المساحة(px)', sheet)
            self.assertIn('r="2"', sheet)
            self.assertNotIn('r="3"', sheet)


if __name__ == '__main__':
    unittest.main()
