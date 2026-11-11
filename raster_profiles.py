"""Safe lossless GeoTIFF creation options for decoded raster samples."""


def lossless_profile(source, **updates):
    profile = dict(source)
    profile.update(updates)
    # GDAL reads JPEG/YCbCr samples as RGB. Never inherit its creation options
    # into a lossless TIFF (including single-band analysis exports).
    for key in list(profile):
        if key.lower() in {'photometric', 'jpeg_quality', 'jpeg_tablesmode',
                           'ycbcr_subsampling', 'nbits', 'predictor'}:
            profile.pop(key)
    # ENVI inputs can carry INTERLEAVE=LINE, which GTiff cannot create.
    # Let GDAL choose its safe default rather than forwarding a source-driver option.
    interleave = str(profile.get('interleave', '')).lower()
    if interleave not in ('pixel', 'band'):
        profile.pop('interleave', None)
    compression = str(profile.get('compress', 'deflate')).lower()
    if compression not in {'deflate', 'lzw'}:
        compression = 'deflate'
    profile.update(driver='GTiff', compress=compression, BIGTIFF='IF_SAFER')
    profile['photometric'] = 'RGB' if profile.get('count') in (3, 4) else 'MINISBLACK'
    return profile
