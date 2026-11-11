import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PySide6.QtWidgets import QApplication, QFileDialog

from image_editor import ImageEditorDialog


class ImageEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        cls.app = QApplication.instance() or QApplication([])

    def test_working_layer_annotations_undo_redo_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            yy, xx = np.mgrid[:48, :64]
            rgb = np.dstack((xx*3, yy*4, (xx+yy)*2)).clip(0,255).astype(np.uint8)
            source = root/'original.png'
            self.assertTrue(cv2.imwrite(str(source), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)))
            source_bytes = source.read_bytes()
            dialog = ImageEditorDialog(source)
            dialog.apply_crop((5, 6, 40, 30))
            self.assertEqual(dialog.base.shape[:2], (30, 40))
            dialog.undo()
            self.assertEqual(dialog.base.shape[:2], (48, 64))
            dialog.redo()
            self.assertEqual(dialog.base.shape[:2], (30, 40))
            dialog.rotate(True)
            self.assertEqual(dialog.base.shape[:2], (40, 30))
            dialog.annotations.append({'id':'label1','type':'text','points':[(5,15)],
                                       'text':'هدف 1','color':'#ff0000','font_size':16})
            dialog.annotations.append({'id':'shape1','type':'rectangle','points':[(2,2),(20,25)],
                                       'color':'#00ff00','size':2})
            composite = dialog._composite()
            self.assertEqual(composite.shape, (40, 30, 3))
            self.assertFalse(np.array_equal(composite, dialog.base))
            output = root/'annotated.png'
            with patch('image_editor.get_storage_path', return_value=root), \
                 patch.object(QFileDialog, 'getSaveFileName', return_value=(str(output), 'PNG (*.png)')), \
                 patch('image_editor.confirm_overwrite', return_value=True):
                dialog.save_layers()
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertTrue(output.is_file())
            self.assertTrue(Path(dialog.result['working_path']).is_file())
            reopened = ImageEditorDialog(source, dialog.result)
            self.assertEqual(reopened.base.shape[:2], (40, 30))
            self.assertEqual(len(reopened.annotations), 2)
            self.assertEqual(reopened.annotations[0]['text'], 'هدف 1')


if __name__ == '__main__':
    unittest.main()
