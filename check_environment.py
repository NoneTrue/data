import sys, struct, importlib

EXPECTED_MAJOR_MINOR = (3, 11)
PACKAGES = [
    ('numpy','numpy'), ('cv2','OpenCV'), ('rasterio','Rasterio'),
    ('pyproj','PyProj'), ('shapely','Shapely'), ('skimage','scikit-image'),
    ('PySide6','PySide6'),
]

print('Python:', sys.version.split()[0])
print('EXE:', sys.executable)
print('BITS:', struct.calcsize('P') * 8)
if sys.version_info[:2] != EXPECTED_MAJOR_MINOR:
    raise SystemExit('FAIL: هذا المشروع معتمد على Python 3.11.x')
if struct.calcsize('P') * 8 != 64:
    raise SystemExit('FAIL: يلزم Python 64-bit')
for module, label in PACKAGES:
    m = importlib.import_module(module)
    print(f'{label}:', getattr(m, '__version__', 'OK'))
print('STATUS: PASS')
