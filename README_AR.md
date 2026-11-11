# ChangeVision 2026 — Analysis Edition

هذه نسخة تحليل مستقلة عن ArcGIS Pro وQGIS Python. كل وظائفها تعمل من نفس Python 3.11 الخاص بالمشروع.

## البيئة المعتمدة
- Python 3.11.x 64-bit
- PySide6 6.11.2
- NumPy 1.26.4
- OpenCV 4.10.0
- Rasterio 1.4.4
- PyProj 3.7.2
- Shapely 2.0.2
- scikit-image 0.22.0

## ما تم تغييره
1. إزالة الاعتماد التشغيلي على ArcGIS Pro / ArcPy / Python الخاص بـArcGIS.
2. حذف تبويب وأزرار ArcGIS من الواجهة واستبدالها بـ GIS Tools تعمل من نفس Python.
3. إصلاح قارئ TIFF/GeoTIFF حتى لا يقرأ Band رابع غير مطلوب في العرض/التحليل RGB، ويحافظ على CRS وTransform وPixel Size.
4. إضافة استرجاع CRS/Transform من ملفات `.aux.xml` و`.tfw` عند الحاجة.
5. إضافة فحص GIS للصورتين: CRS، Pixel Size، Extent، ونسبة التداخل.
6. إضافة قص منطقة التداخل الجغرافي إلى ملفي GeoTIFF بدقة المصدر.
7. إضافة توحيد الصورة الثانية على شبكة الصورة الأولى بواسطة Rasterio: CRS / Pixel Size / Extent.
8. التقارير أصبحت خاصة بالتحليل فقط.
9. التصدير أصبح خاصًا بنتائج التحليل فقط.
10. ROBUST FINAL وإعدادات التحليل الأساسية لم يتم استبدالها بمحرك ArcGIS أو QGIS.

## تشغيل المصدر
من داخل المجلد:

```powershell
py -3.11 check_environment.py
py -3.11 main.py
```

أو شغّل `run_windows.bat`.

## ملاحظة الدقة
أدوات GIS تحفظ النتائج GeoTIFF على شبكة كاملة الدقة. Preview العرض لا يغير دقة ملفات النتائج أو الإحداثيات الأصلية.
