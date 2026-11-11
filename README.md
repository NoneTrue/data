# ChangeVision 2026 — Analysis Edition

تطبيق Windows لتحليل التغيرات بين الصور وإدارة الأهداف وتحرير تقارير Word وPDF.

## التشغيل على Windows

Install **Python 3.11, 64-bit**, then clone or download this repository:

```powershell
git clone https://github.com/NoneTrue/data.git
cd data
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --keyring-provider disabled -r requirements-lock-windows.txt
.\.venv\Scripts\python.exe main.py
```

Alternatively run `setup_windows.ps1`, followed by `run_windows.bat`.
These commands install dependencies in the local virtual environment.

## التحقق الآلي

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe verify.py
.\.venv\Scripts\python.exe verify.py --native --pattern test_region_excel.py
.\.venv\Scripts\python.exe verify.py --native --pattern test_report_target_name.py
```

`verify.py` runs existing tests using private settings and generated data in a
new system temporary directory, outside the application folder. Summaries are
written under `.validation/`, excluded from Git. Child Python processes use private Qt
settings. Production image datasets and projects are not needed.
Each test module runs in a fresh Python process, with a per-module timeout.
OpenCV uses one worker thread during verification to keep test execution
independent of shared GUI/worker thread-pool state; application code is unchanged.

## موارد التشغيل

**OTB, GeoAlignAI, PyTorch and model weights are not required by the current
analysis engine.** The current application uses the Python dependencies in
`requirements.txt`; `requirements-lock-windows.txt` pins the full tested dependency
set for Python 3.11 on Windows. No model inference or GeoAlignAI backend is connected to
the current analysis UI.

Optional runtime folders reported as `MISSING` do not prevent current image
comparison, target management or report editing. The optional development folders
exceed 1 GB locally and contain duplicate weights, training/development files and
third-party repositories. They are not silently downloaded by setup.
See `RUNTIME_PACKAGING_AR.md` for the historical inventory.

## بناء Windows

The validated prebuilt EXE and installer are not stored in Git. To create a new
source build, run `build_exe.bat` in a clean environment. This installs the separate
`requirements-build.txt` and builds `dist/ChangeVision2026/`.
Distribute the entire folder, not the EXE alone.

Compile `installer.iss` using Inno Setup 6 after the one-folder build.
It installs separately as `ChangeVision2026_SourceBuild` and refuses to overwrite
an existing EXE in the selected folder. The build recipe is separate from the
historical validated release; adding it does not certify a newly built installer.

## Source provenance

Application modules are unchanged copies of `RELEASE_20261110_TARGET_EXCEL/source`,
version `2026.11.10.1`. They include report target-name editing and automatic
region-table XLSX saving. Packaging, setup, test-isolation utilities and docs
are maintained separately.

Production images, user projects, generated reports, logs, credentials, EXE files,
installers and large optional development runtimes are excluded.
The original release passed Windows acceptance checks for Word/PDF export,
XLSX autosave and project reopen. See `VALIDATION.md` for fresh-checkout results.

Refer to `README_AR.md` for historical project details; the instructions here
describe the GitHub source checkout.
