"""Run existing tests with private settings and generated data only."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', action='store_true', help='Use the Windows Qt backend')
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--module', help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = ROOT / '.validation'
    output.mkdir(exist_ok=True)
    backend = 'windows' if args.native else 'offscreen'
    if not args.module:
        modules = sorted(p.stem for p in ROOT.glob(args.pattern))
        if not modules:
            parser.error('No test modules match the pattern')
        summaries = []
        for module in modules:
            command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--module', module]
            if args.native:
                command.append('--native')
            started = time.time_ns()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding='utf-8', errors='replace')
            try:
                log, _ = process.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                   capture_output=True)
                else:
                    process.kill()
                log, _ = process.communicate()
                log += '\nWorker exceeded the 600-second test-module limit.\n'
            (output / f'{module}-{backend}.log').write_text(log, encoding='utf-8')
            print(log, end='', flush=True)
            summary_path = output / f'{module}-{backend}.json'
            candidate = (json.loads(summary_path.read_text(encoding='utf-8'))
                         if summary_path.exists() and summary_path.stat().st_mtime_ns >= started else None)
            if candidate and (process.returncode == 0 or candidate['status'] == 'FAIL'):
                summaries.append(candidate)
            else:
                summaries.append({'status': 'FAIL', 'module': module, 'tests': 0,
                                  'errors': 1, 'failures': 0, 'skipped': 0})
        summary = {'status': 'PASS' if all(s['status'] == 'PASS' for s in summaries) else 'FAIL',
                   'backend': backend, 'modules': len(modules),
                   **{k: sum(s[k] for s in summaries) for k in ['tests', 'failures', 'errors', 'skipped']},
                   'results': summaries}
        (output / ('tests-native.json' if args.native else 'tests.json')).write_text(
            json.dumps(summary, indent=2), encoding='utf-8')
        return 0 if summary['status'] == 'PASS' else 1
    data_root = Path(tempfile.mkdtemp(prefix='changevision-verify-')).resolve()
    temp = data_root / 'tmp'
    temp.mkdir(exist_ok=True)
    tempfile.tempdir = str(temp)
    os.environ['TEMP'] = os.environ['TMP'] = str(temp)
    os.environ['QT_QPA_PLATFORM'] = 'windows' if args.native else 'offscreen'
    os.environ['CHANGEVISION_TEST_ROOT'] = str(data_root)
    os.environ['PYTHONPATH'] = os.pathsep.join(filter(None, [str(ROOT), str(ROOT / 'tools'), os.environ.get('PYTHONPATH')]))
    os.chdir(data_root)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(data_root / 'qt-settings'))
    app = QApplication.instance() or QApplication([])
    import cv2
    cv2.setNumThreads(1)
    import storage_manager

    class PrivateResult(unittest.TextTestResult):
        def startTest(self, test):
            case = data_root / 'cases' / hashlib.sha256(test.id().encode()).hexdigest()[:16]
            case.mkdir(parents=True, exist_ok=True)
            self.settings = QSettings(str(case / 'storage.ini'), QSettings.IniFormat)
            self.settings.clear()
            self.home_patch = patch.object(Path, 'home', return_value=case)
            self.storage_patch = patch.object(storage_manager, '_settings', return_value=self.settings)
            self.home_patch.start()
            self.storage_patch.start()
            super().startTest(test)

        def stopTest(self, test):
            self.storage_patch.stop()
            self.home_patch.stop()
            super().stopTest(test)

    suite = unittest.defaultTestLoader.loadTestsFromName(args.module)
    result = unittest.TextTestRunner(verbosity=2, resultclass=PrivateResult).run(suite)
    summary = {'status': 'PASS' if result.wasSuccessful() else 'FAIL',
               'tests': result.testsRun, 'failures': len(result.failures),
               'errors': len(result.errors), 'skipped': len(result.skipped),
               'backend': os.environ['QT_QPA_PLATFORM'], 'python': sys.version,
               'optional_runtimes_present': {name: (ROOT / 'runtime' / name).exists()
                                            for name in ['OTB-9.1.0-Win64', 'GeoAlignAI', 'models']}}
    summary['module'] = args.module
    (output / f'{args.module}-{backend}.json').write_text(
        json.dumps(summary, indent=2), encoding='utf-8')
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__':
    raise SystemExit(main())
