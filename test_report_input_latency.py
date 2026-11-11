import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest,time
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication,QEvent
from report_adapter import from_snapshot
from report_editor import ReportEditorDialog
APP=QApplication.instance() or QApplication([])
class SelectionLatencyTests(unittest.TestCase):
    def test_selection_does_not_copy_whole_document_or_dispatch_twice(self):
        doc,assets=from_snapshot({'regions':[{'id':1,'target_uid':'S1','report_included':True}]})
        dialog=ReportEditorDialog();dialog.loaded((doc,assets));dialog.debounce.stop()
        key=doc.sections_for_view()[1]['uid'];before=doc.to_dict()
        with patch.object(doc,'to_dict',side_effect=AssertionError('Read-only selection must not copy snapshot')),patch.object(dialog,'locate',wraps=dialog.locate) as locate:
            self.assertTrue(dialog.select_uid(key));self.assertEqual(locate.call_count,1)
            self.assertTrue(dialog.select_uid(key));self.assertEqual(locate.call_count,2)
        self.assertEqual(doc.to_dict(),before);dialog.controller.mark_saved();dialog.form_dirty=False;dialog.reject();dialog.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);APP.processEvents()
if __name__=='__main__':unittest.main()
