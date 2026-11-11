# Windows source-checkout validation

Date: **2026-11-11**. Python: **3.11.9, Windows 64-bit**.

The source was cloned from `https://github.com/NoneTrue/data` into a separate
checkout, then installed into a new virtual environment created without
`--system-site-packages`. All application Python dependencies were confirmed
to resolve inside that environment. `pip check` reported no broken requirements.
The full set of 27 installed packages is pinned in `requirements-lock-windows.txt`.

| Check | Result |
| --- | --- |
| Existing test modules, offscreen Qt | **PASS: 129 tests in 20 modules**, 0 failures/errors/skips |
| Native Windows Qt region-table/XLSX tests | **PASS: 13 tests** |
| Native Windows Qt report target-name tests | **PASS: 3 tests** |
| Moved source checkout with spaces in its path | **PASS** |
| Main window, synthetic GeoTIFF, geospatial data, alignment and image comparison smoke checks | **PASS**, through the moved-source runtime test |
| Syntax of setup PowerShell script and Python/build specification files | **PASS** |

OTB, GeoAlignAI, model directories and PyTorch were **absent** during these tests.
They are not required by the currently connected analysis or report workflows.

## What the tests cover

The tests exercise synthetic image import, preparation, georeferencing, coordinate
conversion, structural comparison, alignment quality gates, change detection,
report-table target-name editing, undo/redo, report save/reopen, Word/PDF export,
region XLSX autosave, session restore, file-lock retry, read-only files, formula
protection and preservation of analytical fields.

Each module runs in a separate subprocess with private Qt settings and temporary
data outside the application directory. OpenCV is limited to one worker thread
for verification. Existing test fixtures were corrected to locate source files
relative to `__file__`, register Arial explicitly, and write diagnostic output
outside the application directory. The three affected modules were rechecked
successfully after those fixture corrections. Application modules were unchanged.

Local summaries and logs are under `.validation/` and excluded from Git.
GitHub Actions repeats the tests on a separate Windows runner; its live status is
shown on the repository Actions page. Local success does not itself claim that a
remote workflow has completed successfully.

## Limits

This validates the source checkout and its current connected workflows. No new
EXE or installer was built or installed during this validation. The supplied build
and installer recipes are for a separate source build and do not alter the
historical validated release or an existing user installation.

The large optional development runtimes, user datasets, custom model weights,
production projects and generated reports are deliberately outside this repository.
