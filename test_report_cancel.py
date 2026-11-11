import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import io,tempfile,unittest,threading
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from report_adapter import from_snapshot
from report_cancel import CancellationToken,ReportCancelled,cancellation_scope,checkpoint,commit_file
from report_docx import export_docx
from report_layout import export_pdf,ReportLayout
from report_project import save_project,load_project
APP=QApplication.instance() or QApplication([])

class CancelTests(unittest.TestCase):
    def setUp(self):self.doc,self.assets=from_snapshot({'regions':[{'id':1,'target_uid':'C1','report_included':True}]})
    def test_pre_cancelled_exports_do_not_create_files(self):
        with tempfile.TemporaryDirectory() as temp:
            token=CancellationToken();token.cancel()
            for ext,fn in [('docx',export_docx),('pdf',export_pdf),('cvreport',save_project)]:
                with self.assertRaises(ReportCancelled):fn(Path(temp)/('out.'+ext),self.doc,self.assets,cancel=token)
            self.assertEqual(list(Path(temp).iterdir()),[])
    def test_archive_write_cancellation_preserves_previous_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.docx';path.write_bytes(b'PREVIOUS');token=CancellationToken()
            def stop(stage=None):
                if stage=='archive_write':token.cancel()
                checkpoint(stage)
            with patch('report_docx.checkpoint',side_effect=stop),patch('report_cancel.checkpoint',side_effect=lambda stage=None:token.check(stage)):
                # CheckedWriter calls report_cancel.checkpoint directly.
                from report_cancel import CheckedWriter
                original=CheckedWriter.write
                def writing(stream,data):token.cancel();return original(stream,data)
                with patch.object(CheckedWriter,'write',writing),self.assertRaises(ReportCancelled):export_docx(path,self.doc,self.assets,overwrite=True,cancel=token)
            self.assertEqual(path.read_bytes(),b'PREVIOUS');self.assertEqual([p.name for p in Path(temp).iterdir()],['out.docx'])
    def test_pdf_page_cancellation_releases_writer_and_preserves_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.pdf';path.write_bytes(b'PREVIOUS');token=CancellationToken()
            def stop(stage=None):
                if stage=='pdf_page':token.cancel()
                token.check(stage)
            with patch('report_layout.checkpoint',side_effect=stop),self.assertRaises(ReportCancelled):export_pdf(path,self.doc,self.assets,overwrite=True,cancel=token)
            self.assertEqual(path.read_bytes(),b'PREVIOUS');self.assertEqual([p.name for p in Path(temp).iterdir()],['out.pdf'])
    def test_cancel_at_last_checkpoint_preserves_saved_project(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.cvreport';save_project(path,self.doc,self.assets);old=path.read_bytes();token=CancellationToken()
            def stop(stage=None):
                if stage=='project_before_commit':token.cancel()
                token.check(stage)
            with patch('report_project.checkpoint',side_effect=stop),self.assertRaises(ReportCancelled):save_project(path,self.doc,self.assets,overwrite=True,cancel=token)
            self.assertEqual(path.read_bytes(),old);self.assertEqual(load_project(path)[0].to_dict(),self.doc.to_dict());self.assertEqual(len(list(Path(temp).iterdir())),1)
    def test_cancel_at_last_word_checkpoint_preserves_previous(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.docx';path.write_bytes(b'PREVIOUS');token=CancellationToken()
            def stop(stage=None):
                if stage=='docx_before_commit':token.cancel()
                token.check(stage)
            with patch('report_docx.checkpoint',side_effect=stop),self.assertRaises(ReportCancelled):export_docx(path,self.doc,self.assets,overwrite=True,cancel=token)
            self.assertEqual(path.read_bytes(),b'PREVIOUS');self.assertEqual(len(list(Path(temp).iterdir())),1)
    def test_commit_in_progress_rejects_cancel_without_waiting(self):
        token=CancellationToken();entered=threading.Event();release=threading.Event()
        def commit():entered.set();release.wait(3)
        worker=threading.Thread(target=lambda:token.commit(commit));worker.start();self.assertTrue(entered.wait(1))
        try:self.assertFalse(token.cancel())
        finally:release.set();worker.join()
        self.assertEqual(token.state,'committed');self.assertFalse(token.cancel());token.finish()

if __name__=='__main__':unittest.main()
