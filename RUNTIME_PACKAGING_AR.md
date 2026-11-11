# إدارة runtime وتغليف نسخة التحليل

هذه التغييرات تخص المسارات والأيقونة والتغليف فقط. لم تتغير دوال Change Detection أو Alignment أو Radiometric أو GIS. النسخ الأصلية للصور والأيقونات والمكتبات الخارجية محفوظة.

## المسارات والنتائج

`runtime_paths.py` هو المرجع المركزي. في Python يستخدم مجلد المصدر، وفي EXE يستخدم مجلد `sys.executable` للـruntime الدائم. موارد حزم Python تأتي من `_internal`/`_MEIPASS`، ولا تستخدم الأخيرة لحفظ ملفات المستخدم. التمييز مطابق [لتوثيق PyInstaller](https://pyinstaller.org/en/stable/runtime-information.html).

التقارير ونسخ التحضير وExports تذهب إلى `Documents/ChangeVision2026`، ويمكن للمستخدم اختيار وجهة التصدير كما سابقًا. لا تكتب نسخة التحضير أو التصدير الافتراضي بجانب EXE داخل Program Files.

## الاعتماديات وتصنيفها

| التصنيف | الملفات |
|---|---|
| MUST_COPY_TO_RUNTIME | الأيقونة الرسمية وملفات الأيقونة المساندة، من النسخ الموجودة أصلًا في runtime؛ لا حاجة لمصدر خارجي |
| PYINSTALLER_WILL_BUNDLE | Python 3.11 runtime وPySide6/Qt وOpenCV وNumPy وSciPy وscikit-image وRasterio/GDAL وPyProj/PROJ وShapely واعتمادياتها الفعلية |
| OPTIONAL_EXTERNAL | OTB وGeoAlignAI وملفات النماذج غير المستخدمة؛ غيابها لا يعطل نسخة التحليل |
| TRAINING_OR_DEVELOPMENT_ONLY | OTB include/otbtf/build/source، مستودعات وأوزان GeoAlignAI غير المستدعاة حاليًا، بيانات التدريب، الاختبارات، السجلات والنسخ الاحتياطية |
| DO_NOT_COPY | Windows/System32، تثبيت Python أو Conda كاملًا، QGIS وArcGIS وCUDA Toolkit وVisual Studio |

لا يوجد تحميل نماذج أو استدعاء OTB/GeoAlignAI داخل خوارزمية التحليل الحالية. تم توفير المسارات والبيئة والفحص، ولم يضف مسار استدلال أو محرك مواءمة آخر. مجلد `runtime/models` غير موجود وقت هذا الفحص؛ لا يُنشأ فارغًا ولا يُستعاد تلقائيًا.

GeoAlignAI الموجود يحتوي مستودعات SuperPoint وSuperGlue وLoFTR وأوزانها، وليس خدمة/محركًا موصولًا بالواجهة الحالية. PyTorch موجود في بيئة التطوير، بينما Kornia وeinops غير موجودين. لا تثبت متطلبات التدريب القديمة أو تنسخها إلى Setup. هذه الملفات غير مشمولة تلقائيًا في الحزمة لعدم استخدامها؛ يلزم تكامل مستقل واختبار اعتمادياته وحقوق توزيعه قبل شحنه.

## GDAL وPROJ وOTB

المصدر التشغيلي لـGDAL هو Rasterio، وليس QGIS أو OSGeo. يستخدم Rasterio `rasterio/gdal_data` و`rasterio/proj_data`، ويضبط PyProj مساره الخاص إلى `pyproj/proj_dir/share/proj`. لا تُخلط قواعد بيانات PROJ بين نسخ المكتبات.

OTB يستعمل بيئة منفصلة لعمليته الفرعية: `bin` و`lib` في PATH، و`lib/otb/applications` للتطبيقات، و`share/data` و`share/proj` لبياناته. لا يحقن DLLs أو Python modules الخاصة به في عملية Qt. أصلح `otbenv1.bat` ليستخدم مكانه الحالي، وأصلح `otbenv.ps1` إلى مجلد `applications` الصحيح ومتغيرات مقتبسة ومسار PROJ_DATA.

فحص استيرادات launcher وReadImageInfo وتبعياتهما شمل 49 ملفًا ثنائيًا؛ المراجع غير الموجودة في OTB تخص مكتبات Windows النظامية/API sets. تفاصيل الفحص في `validation/runtime_binary_audit.json`.

## الأيقونات والبناء

تم نسخ الأيقونات الثلاث من `runtime` إلى `runtime/icons` مع إبقاء الأصل والتحقق من SHA256. الرسمية: `ChangeVision2026_HQ.ico`. تطبق على QApplication وMainWindow، ويُضبط AppUserModelID لشريط المهام. غيابها يسمح بالـPNG البديل أو التشغيل دون أيقونة.

`ChangeVision2026.spec` يبني One-Folder باسم `ChangeVision2026.exe`؛ `package_runtime.py` ينسخ موارد runtime ويتحقق من بصماتها. خيار `--include-otb` يضم bin/lib/share ورخصة OTB وملفات البيئة فقط، بحجم نحو 359 MB قبل الضغط، دون شجرة تطوير otbtf.

اكتُشف تلوث في نتائج البحث عن DLL أثناء البناء بسبب Miniforge في بيئة التطوير. إعداد spec يستبعد المكتبات الأجنبية أو يستبدل الأسماء المتطابقة بمكتبات بيئة Python المعتمدة، وخاصة Qt من PySide6. يسجل القرارات في `validation/build_dependency_resolution.json`. لا يعدّل تثبيت Python أو Miniforge الأصلي.

`installer.iss` يجهز تثبيت One-Folder والأيقونات واختصارات سطح المكتب وقائمة ابدأ. استخدام One-Folder وبقاء runtime بجانب EXE يتبع [آلية ملفات spec](https://pyinstaller.org/en/stable/spec-files.html). لا تُضم بيانات التدريب أو النتائج أو النسخ الاحتياطية. يلزم توزيع المجلد كاملًا، وليس ملف EXE وحده.

للبناء، استخدم Python 3.11 المختار ثم:

```text
python -m PyInstaller --noconfirm ChangeVision2026.spec
python package_runtime.py dist/ChangeVision2026 --include-otb
ISCC installer.iss
```

يمكن ضبط `CHANGEVISION_PYTHON` لمسار Python المختار في build_exe.bat/run_windows.bat؛ لا يُحفظ هذا المسار في كود الإنتاج.

## إعادة الفحص

```text
python -X utf8 runtime_diagnostics.py validation/runtime_scan.json --smoke --scan
python -X utf8 -m unittest test_runtime_paths test_preparation test_radiometric_inspection test_structural_workflow test_regression -q
ChangeVision2026.exe --runtime-diagnostics diagnostic.json
```

الفحص التشغيلي يقرأ/يكتب GeoTIFF اصطناعيًا، يحوّل UTM إلى WGS84، يفتح ويغلق نافذة Qt خارج الشاشة، ويفحص ReadImageInfo عند وجود OTB. لا يحمل نماذج أو صور المستخدم. مسارات التطوير الشخصية في ملفات التقارير/الاختبارات وملفات بناء الطرف الثالث توثق مصادرها، ولا تستخدم كمسارات إنتاج ثابتة.

## الملفات المعدلة والمضافة

- تعديل `main.py` و`workflow_ui.py` للمسارات والأيقونة والفحص التشخيصي فقط.
- إضافة `runtime_paths.py` و`runtime_diagnostics.py` و`package_runtime.py` و`test_runtime_paths.py`.
- إضافة `ChangeVision2026.spec` و`installer.iss` وتحديث `build_exe.bat` و`run_windows.bat`.
- تعديل `runtime/OTB-9.1.0-Win64/otbenv1.bat` و`otbenv.ps1` فقط من ملفات الطرف الثالث.
- نسخ الأيقونات الثلاث إلى `runtime/icons` دون حذف نسخها السابقة.
- تحديث `CHANGELOG_ANALYSIS_ONLY_AR.txt` وإضافة هذا التقرير وأدوات ونتائج التدقيق داخل `validation`.
- المخرجات المبنية داخل `dist/ChangeVision2026` والمثبت داخل `installer_output`؛ لم تُنسخ ملفات تطوير/أوزان غير مستخدمة إلى حزمة التحليل.

## نتائج التحقق

- اختبارات المصدر: 31 ناجحة (5 للمسارات و26 للوظائف السابقة). إعادة اختبار المسارات بعد الفحص الأخير ناجحة أيضًا.
- تشغيل الواجهة من Python، وGeoTIFF read/write في Rasterio وOpenCV، وتحويل الإحداثيات في PyProj: PASS.
- SIFT/المواءمة السابقة: Reprojection نحو 0.122 px، Residual P90 نحو 0.050 px، ECC نحو 0.9444، Mesh=0. بقي OpenCV 4.10.0 في EXE.
- اختبار نقل المصدر مع المسافات وغياب OTB/GeoAlignAI: PASS.
- اختبار EXE المنقول كاملًا إلى مسار بمسافات مع PATH يقتصر على System32: PASS؛ Qt والأيقونة وGDAL/PROJ وقراءة OTB لصورة GeoTIFF اصطناعية نجحت. الأدلة في `validation/exe_relocation.json`.
- موارد الأيقونة RT_ICON وRT_GROUP_ICON داخل EXE موجودة. Qt حمّل الأيقونة؛ لم يجر اختبار بصري مستقل لذاكرة أيقونات Windows أو الاختصارات المثبتة على جهاز نظيف.
- صياغة `otbenv.ps1` سليمة (0 أخطاء parser). سياسة Windows تمنع تنفيذ .ps1 في هذه البيئة؛ لم تغيّر السياسة. التطبيق لا يحتاج تشغيل هذا الملف، ويستخدم بيئة OTB المركزية المختبرة.
- ملفات المصادر الأصلية خارج المشروع لم تُنقل أو تُحذف. النسخ إلى `_internal` جزء من PyInstaller، وليس نسخ تثبيت Python/Conda كاملًا.
- الفحص النهائي أثبت تطابق بصمات وحدات التحليل الثماني مع بصماتها السابقة، و0 مسارات أقراص ثابتة في ثوابت كود التطبيق التشغيلي، ونجاح syntax للملفات الرئيسية الـ19. التقرير: `validation/runtime_final_report.json`.
- نجح بناء EXE النهائي واختباره، بما في ذلك Shapely intersection وSSIM وSIFT داخل الحزمة المنقولة. المجلد الكامل: `dist/ChangeVision2026`.
- نجح تجميع `installer_output/ChangeVision2026_Setup.exe` بواسطة Inno Setup، بحجم 172,007,095 bytes. لم يتم تثبيته على جهاز نظيف أو تعديل تسجيل Windows/اختصاراته أثناء الاختبار. الاختصارات معدة بالأيقونة الرسمية وAppUserModelID المطابق للتطبيق وفق [توثيق Inno Setup](https://jrsoftware.org/ishelp/topic_iconssection.htm).
