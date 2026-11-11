"""Conservative image location/date extraction and opt-in reverse geocoding."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path


def _ratio(value):
    try:
        return float(value.numerator) / float(value.denominator)
    except AttributeError:
        try:
            return float(value[0]) / float(value[1])
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            return float(value)


def _dms(value, reference):
    degrees, minutes, seconds = (_ratio(part) for part in value)
    coordinate = degrees + minutes / 60 + seconds / 3600
    if str(reference).upper() in ('S', 'W'):
        coordinate = -coordinate
    return coordinate


def extract_image_metadata(path):
    """Return only facts supported by EXIF or geospatial raster metadata.

    GeoTIFF coordinates describe raster coverage and its centroid; they are
    deliberately never labeled as a camera capture point.
    """
    path = Path(path)
    result = {'path': str(path), 'source': 'none', 'capture_date': '',
              'longitude': None, 'latitude': None, 'location': '',
              'location_kind': '', 'crs': '', 'bounds_wgs84': None}
    try:
        from PIL import Image, ExifTags
        with Image.open(path) as image:
            exif = image.getexif()
            names = {ExifTags.TAGS.get(k, str(k)): v for k, v in exif.items()}
            date = names.get('DateTimeOriginal') or names.get('DateTimeDigitized')
            if date:
                result['capture_date'] = str(date).replace(':', '-', 2)
                result['source'] = 'EXIF'
            try:
                gps_raw = exif.get_ifd(34853)
            except (AttributeError, KeyError, TypeError):
                gps_raw = exif.get(34853)
            if gps_raw:
                gps = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in gps_raw.items()}
                if all(k in gps for k in ('GPSLatitude', 'GPSLatitudeRef', 'GPSLongitude', 'GPSLongitudeRef')):
                    result.update(latitude=_dms(gps['GPSLatitude'], gps['GPSLatitudeRef']),
                                  longitude=_dms(gps['GPSLongitude'], gps['GPSLongitudeRef']),
                                  location_kind='camera_capture_point', source='EXIF GPS')
    except Exception:
        pass

    try:
        import rasterio
        from pyproj import Transformer
        with rasterio.open(path) as dataset:
            if dataset.crs:
                result['crs'] = dataset.crs.to_string()
                to_wgs84 = Transformer.from_crs(dataset.crs, 'EPSG:4326', always_xy=True)
                x = (dataset.bounds.left + dataset.bounds.right) / 2
                y = (dataset.bounds.bottom + dataset.bounds.top) / 2
                lon, lat = to_wgs84.transform(x, y)
                west, south = to_wgs84.transform(dataset.bounds.left, dataset.bounds.bottom)
                east, north = to_wgs84.transform(dataset.bounds.right, dataset.bounds.top)
                result.update(longitude=float(lon), latitude=float(lat),
                              bounds_wgs84=[float(west), float(south), float(east), float(north)],
                              location_kind='raster_coverage_centroid', source='Raster CRS/Bounds')
    except Exception:
        pass
    if result['longitude'] is not None and result['latitude'] is not None:
        kind = 'مركز تغطية الراستر' if result['location_kind'] == 'raster_coverage_centroid' else 'نقطة التقاط EXIF'
        result['location'] = f"{kind}: {result['latitude']:.6f}, {result['longitude']:.6f}"
    return result


def reverse_geocode(longitude, latitude, cache_path, *, timeout=8):
    """Resolve a coordinate only when explicitly requested by the user.

    Results are cached locally. This optional call is never part of image
    import, analysis, or project loading.
    """
    lon, lat = float(longitude), float(latitude)
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError('الإحداثيات خارج النطاق الجغرافي الصحيح.')
    cache_path = Path(cache_path)
    key = f'{lat:.5f},{lon:.5f}'
    try:
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    query = urllib.parse.urlencode({'format': 'jsonv2', 'lat': lat, 'lon': lon,
                                    'zoom': 18, 'addressdetails': 1})
    request = urllib.request.Request(
        'https://nominatim.openstreetmap.org/reverse?' + query,
        headers={'User-Agent': 'ChangeVision2026/1.0 (optional user-requested lookup)',
                 'Accept-Language': 'ar,en'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8'))
    address = data.get('address') or {}
    place = {key: address[key] for key in ('country', 'state', 'province', 'region',
             'city', 'town', 'village', 'municipality', 'suburb', 'neighbourhood', 'road')
             if address.get(key)}
    value = {'display_name': data.get('display_name', ''), 'address': place,
             'source': 'OpenStreetMap Nominatim', 'longitude': lon, 'latitude': lat}
    cache[key] = value
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        pass
    return value
