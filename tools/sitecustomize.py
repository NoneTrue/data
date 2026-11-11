"""Keep subprocesses spawned by verify.py away from production Qt settings."""
import os
from pathlib import Path

root = os.environ.get('CHANGEVISION_TEST_ROOT')
if root:
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                      str(Path(root) / 'child-qt-settings'))
    settings = QSettings('ChangeVision2026', 'AnalysisEdition')
    settings.setValue('storage/root', str(Path(root) / 'child-data'))
    settings.sync()
