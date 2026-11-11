"""Metadata-driven preparation. Originals are read-only; unknowns stay unknown."""
from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.windows import Window
from raster_profiles import lossless_profile


class Cancelled(RuntimeError):
    pass


def check_cancel(cancel=None):
    if cancel and cancel():
        raise Cancelled('تم إيقاف العملية؛ لم يتم اعتماد نتيجة جديدة.')


def _tags(ds, band=0):
    result = dict(ds.tags(band))
    for ns in ds.tag_namespaces(band):
        result.update({f'{ns}:{k}': v for k, v in ds.tags(band, ns=ns).items()})
    return result


def _semantic(text, tags):
    text = str(text).lower()
    for name, pattern in [('DEM', r'\b(dem|dtm|dsm|elevation|digital elevation model)\b'),
                          ('NIR', r'\b(nir|near[ _-]?infrared)\b'),
                          ('Panchromatic', r'\b(pan|panchromatic)\b'),
                          ('Red', r'\b(red)\b'), ('Green', r'\b(green)\b'),
                          ('Blue', r'\b(blue)\b'), ('Grayscale', r'\b(gray|grey|grayscale|greyscale|intensity)\b')]:
        if re.search(pattern, text):
            return name
    # Interpret wavelength only with explicit units, never infer nm vs microns.
    lower = {str(k).lower(): str(v).lower() for k, v in tags.items()}
    for key, value in lower.items():
        if 'wavelength' not in key or 'unit' in key:
            continue
        unit = ' '.join(v for k, v in lower.items() if 'wavelength' in k and 'unit' in k)
        match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(nm|um|µm|μm)?\s*', value)
        if not match:
            continue
        unit = match.group(2) or unit
        wl = float(match.group(1))
        if unit in ('um', 'µm', 'μm', 'micrometers', 'micrometres'):
            wl *= 1000
        elif unit not in ('nm', 'nanometers', 'nanometres'):
            continue
        if 760 <= wl <= 1100:
            return 'NIR'
        if 620 <= wl <= 700:
            return 'Red'
        if 500 <= wl <= 570:
            return 'Green'
        if 440 <= wl <= 490:
            return 'Blue'
    return 'Unknown'


def inspect_raster(path, declaration=None):
    declaration = declaration or {}
    with rasterio.open(path) as ds:
        tags = _tags(ds)
        gcps, gcp_crs = ds.gcps
        crs, transform = ds.crs, ds.transform
        sidecars = [p for p in (str(path)+'.aux.xml', str(Path(path).with_suffix('.tfw')),
                               str(Path(path).with_suffix('.tifw')), str(path)+'w',
                               str(Path(path).with_suffix('.prj'))) if os.path.isfile(p)]
        # Recover only explicit georeferencing. A world file cannot supply a CRS.
        import engine
        side_crs, side_transform = engine._recover_sidecar_georef(str(path))
        recovered = False
        if crs is None and side_crs is not None:
            crs = side_crs; recovered = True
        if transform.is_identity and side_transform is not None:
            transform = side_transform; recovered = True
        if crs is None:
            prj = Path(path).with_suffix('.prj')
            if prj.is_file():
                try:
                    crs = rasterio.crs.CRS.from_wkt(prj.read_text(encoding='utf-8-sig'))
                    recovered = True
                except (ValueError, rasterio.errors.CRSError):
                    pass
        if crs is None and gcp_crs is not None:
            crs=gcp_crs
        types, band_tags, bits = [], [], []
        global_type = _semantic(' '.join(f'{k}={v}' for k, v in tags.items()), tags)
        ci_types = {ColorInterp.red: 'Red', ColorInterp.green: 'Green', ColorInterp.blue: 'Blue',
                    ColorInterp.alpha: 'Alpha'}
        # TIFF's default Gray color interpretation describes display, not the
        # spectral channel: an unnamed single band may be PAN, NIR or elevation.
        # Require an explicit description/tag or the user's declaration.
        for i in range(1, ds.count+1):
            bt = _tags(ds, i); band_tags.append(bt)
            semantic = _semantic((ds.descriptions[i-1] or '') + ' ' +
                                 ' '.join(f'{k}={v}' for k, v in bt.items()), bt)
            if semantic == 'Unknown' and ds.count == 1 and global_type != 'Unknown':
                semantic = global_type
            types.append(semantic if semantic != 'Unknown' else ci_types.get(ds.colorinterp[i-1], 'Unknown'))
            nbits = next((v for k, v in {**tags, **bt}.items() if k.upper().split(':')[-1] == 'NBITS'), None)
            bits.append(int(nbits) if nbits is not None and str(nbits).isdigit() else np.dtype(ds.dtypes[i-1]).itemsize*8)
        rgb = [types.index(t)+1 for t in ('Red','Green','Blue')] if all(types.count(t)==1 for t in ('Red','Green','Blue')) else None
        explicit = False
        if declaration.get('rgb'):
            mapped = list(declaration['rgb'])
            if len(mapped) != 3 or len(set(mapped)) != 3 or any(not isinstance(i,int) or not 1 <= i <= ds.count for i in mapped):
                raise ValueError('Band mapping غير صالح: أدخل ثلاثة أرقام مختلفة داخل عدد الحزم.')
            for expected,i in zip(('Red','Green','Blue'),mapped):
                if types[i-1] not in ('Unknown','Grayscale',expected):
                    raise ValueError('التعيين يتعارض مع نوع حزمة معروف؛ لا تُدخل NIR أو DEM أو Alpha ضمن RGB.')
            rgb = mapped; explicit = True
        if ds.count == 1 and declaration.get('type'):
            if declaration['type'] not in ('Grayscale','Panchromatic','NIR','Other'):
                raise ValueError('نوع الحزمة المحدد غير مدعوم.')
            types[0] = declaration['type']; explicit = True
        non_alpha = [i+1 for i, t in enumerate(types) if t != 'Alpha']
        if 'DEM' in types or global_type == 'DEM':
            band_type = 'DEM'
        elif rgb:
            band_type = 'RGB' if len(non_alpha) == 3 else 'Multispectral'
        elif len(non_alpha) == 1:
            band_type = types[non_alpha[0]-1]
        elif all(t != 'Unknown' for t in types):
            band_type = 'Multispectral'
        else:
            band_type = 'Unknown'
        if tags.get('ANALYSIS_MODE') == 'STRUCTURAL COMPARISON' and tags.get('STRUCTURAL_VERSION') == '1' and ds.count == 3:
            band_type = 'Structural'
        scale = min(1., 384/max(ds.width, ds.height))
        data = ds.read(out_shape=(ds.count, max(1,round(ds.height*scale)), max(1,round(ds.width*scale))),
                       masked=True, resampling=Resampling.nearest)
        raw = np.asarray(data)
        valid = ~np.any(np.ma.getmaskarray(data),axis=0) & np.all(np.isfinite(raw),axis=0)
        idx = np.array(non_alpha or list(range(1,ds.count+1))) - 1
        black = np.all(raw[idx] == 0,axis=0)
        ranges = []
        for band in raw:
            values = band[valid & ~black]
            ranges.append([float(x) for x in np.percentile(values,(2,50,98))] if values.size else None)
        det = transform.a*transform.e-transform.b*transform.d
        usable_transform = bool(np.isfinite(tuple(transform)[:6]).all() and abs(det)>1e-20
                                and (not transform.is_identity or side_transform is not None))
        corners = [transform*p for p in ((0,0),(ds.width,0),(ds.width,ds.height),(0,ds.height))]
        return dict(path=str(Path(path).resolve()), crs=crs.to_string() if crs else None,
                    epsg=crs.to_epsg() if crs else None,
                    crs_usable=bool(crs and (crs.is_geographic or crs.is_projected)),
                    transform=list(transform)[:6], transform_usable=usable_transform,
                    origin=[transform.c,transform.f], pixel_size=[float(np.hypot(transform.a,transform.d)),
                                                               float(np.hypot(transform.b,transform.e))],
                    rotation_skew=[transform.b,transform.d],
                    axis_direction=f'column vector=({transform.a:g},{transform.d:g}); row vector=({transform.b:g},{transform.e:g})',
                    bounds=[min(p[0] for p in corners),min(p[1] for p in corners),max(p[0] for p in corners),max(p[1] for p in corners)],
                    width=ds.width,height=ds.height,bands=ds.count,band_type=band_type,band_types=types,
                    rgb=rgb, non_alpha=non_alpha, dtypes=list(ds.dtypes),bit_depth=bits,
                    dynamic_range=ranges, descriptions=list(ds.descriptions),colorinterp=[c.name for c in ds.colorinterp],
                    band_tags=band_tags,tags=tags,gcps=len(gcps),gcp_crs=str(gcp_crs) if gcp_crs else None,
                    sidecars=sidecars,recovered_georef=recovered,
                    nodata=list(ds.nodatavals),valid_percent=float(100*valid.mean()),black_percent=float(100*black.mean()),
                    declaration=declaration, user_declared=explicit)


def band_compatibility(a,b):
    ta,tb = a['band_type'],b['band_type']
    if 'DEM' in (ta,tb):
        return dict(status='NOT READY',mode='INCOMPATIBLE FOR NORMAL OPTICAL CHANGE DETECTION',reason='DEM لا يستخدم في كشف التغير الضوئي.')
    if ta == tb == 'Structural':
        return dict(status='READY',mode='STRUCTURAL COMPARISON',reason='أوصاف بنيوية متعددة المقاييس؛ النتائج مرشحات للمراجعة')
    if 'Structural' in (ta,tb):
        return dict(status='NOT READY',mode='STRUCTURAL COMPARISON',reason='يلزم اشتقاق أوصاف بنيوية للصورتين معًا.')
    if (ta == 'NIR' and b['rgb']) or (tb == 'NIR' and a['rgb']):
        return dict(status='NEEDS HARMONIZATION',mode='STRUCTURAL COMPARISON',reason='RGB ↔ NIR: استخراج البنية؛ يمنع فرق الشدة والتوحيد الإشعاعي العادي')
    if 'NIR' in (ta,tb) and ta != tb:
        return dict(status='NOT READY',mode='RADIOMETRICALLY DIFFERENT',reason='زوج الحزم غير مدعوم في المقارنة الضوئية العادية.')
    single = {'Grayscale','Panchromatic','NIR'}
    ca,cb = bool(a['rgb']),bool(b['rgb'])
    if not ((ca or ta in single) and (cb or tb in single)):
        return dict(status='NOT READY',mode='Unknown',reason='Band mapping required — نوع الحزمة غير معروف؛ حدده دون تغيير Metadata الأصلية.')
    mode = 'NORMAL OPTICAL COMPARISON' if ca and cb else ('Intensity ↔ Panchromatic / Grayscale' if ca != cb else 'SINGLE BAND COMPARISON')
    canonical = lambda m: (m['rgb'] == [1,2,3] and m['bands'] == 3) if m['rgb'] else m['bands'] == 1
    needs = ca != cb or not canonical(a) or not canonical(b) or set(a['bit_depth']) != set(b['bit_depth'])
    needs |= any(dt != 'uint8' for m in (a,b) for dt in m['dtypes'])
    range_mismatch=False
    if not ca and not cb and a['dynamic_range'][0] and b['dynamic_range'][0]:
        ra,rb=a['dynamic_range'][0],b['dynamic_range'][0]
        sa,sb=ra[2]-ra[0],rb[2]-rb[0]
        range_mismatch=max(sa,sb)>2*max(min(sa,sb),1e-9)
        needs |= range_mismatch
    return dict(status='NEEDS HARMONIZATION' if needs else 'READY',mode=mode,
                normalize=range_mismatch,
                reason='توحيد الحزم للتحليل' if needs else 'الحزم المشتركة متوافقة')


def overlap(a,b):
    from affine import Affine
    from shapely.geometry import Polygon
    from pyproj import Transformer
    def ring(info):
        t = Affine(*info['transform']); w,h=info['width'],info['height']
        points=[]
        for p,q in [((0,0),(w,0)),((w,0),(w,h)),((w,h),(0,h)),((0,h),(0,0))]:
            points.extend(t*(p[0]+v*(q[0]-p[0]),p[1]+v*(q[1]-p[1])) for v in np.linspace(0,1,33))
        return points
    pa,pb=ring(a),ring(b)
    if a['crs'] != b['crs']:
        tr=Transformer.from_crs(b['crs'],a['crs'],always_xy=True)
        pb=[tr.transform(*p,errcheck=True) for p in pb]
    x,y=Polygon(pa),Polygon(pb)
    area=x.intersection(y).area
    return dict(percent_a=100*area/max(x.area,1e-30),percent_b=100*area/max(y.area,1e-30),exists=area>0)


def readiness(a,b,alignment=None,radiometric=None,radiometric_accepted=False,analysis_mode=None):
    if a is None or b is None:
        return dict(status='NOT READY',reason='افتح الصورتين أولًا.',action='open',compatibility={},overlap=None)
    comp=band_compatibility(a,b)
    result=dict(status='NEEDS PROCESSING',reason='',action=None,compatibility=comp,overlap=None,
                analysis_mode=analysis_mode or comp['mode'])
    def state(status,reason,action=None):
        return dict(result,status=status,reason=reason,action=action)
    if not a['crs_usable'] or not b['crs_usable']:
        return state('NOT READY','CRS مفقود ولا يمكن تحديده بأمان' if not a['crs'] or not b['crs'] else 'CRS محلي أو غير صالح لإحداثيات جغرافية؛ يلزم تعريف موثوق.')
    if not a['transform_usable'] or not b['transform_usable']:
        return state('NOT READY','GeoTransform غير صالح؛ راجع GCPs / World File / Metadata قبل المقارنة.')
    if a.get('recovered_georef') or b.get('recovered_georef'):
        return state('NEEDS PROCESSING','إحداثيات موثقة في Sidecar؛ أنشئ نسخة GeoTIFF تحليلية قبل المعالجة','metadata_copy')
    try:
        result['overlap']=overlap(a,b)
    except Exception as exc:
        return state('NOT READY','تعذر حساب التداخل بأمان: '+str(exc))
    if not result['overlap']['exists']:
        return state('NOT READY','لا يوجد تداخل جغرافي بين الصورتين.')
    if result['overlap']['percent_a'] < 55:
        return state('NEEDS PROCESSING','التداخل يغطي أقل من 55% من الصورة 1؛ يوصى بقص منطقة التداخل قبل توحيد الشبكة','crop')
    if a['crs'] != b['crs']:
        return state('NEEDS PROCESSING','تحتاج إعادة إسقاط الصورة 2 إلى CRS الصورة 1','reproject')
    if (a['width'],a['height']) != (b['width'],b['height']) or not np.allclose(a['transform'],b['transform'],atol=1e-12,rtol=0):
        return state('NEEDS PROCESSING','تحتاج توحيد الصورة 2 على شبكة الصورة 1','grid')
    if comp['status']=='NOT READY':
        return state('NOT READY',comp['reason'],'bands' if comp['mode']=='Unknown' else None)
    if comp['status']=='NEEDS HARMONIZATION':
        return state('NEEDS PROCESSING',comp['reason'],'harmonize')
    r=alignment or {}
    finite=lambda k: isinstance(r.get(k),(float,int)) and np.isfinite(r[k])
    good=(r.get('alignment_approved') and all(finite(k) for k in ('reprojection_error_px','residual_p90_px','ecc_score','inlier_ratio'))
          and r['reprojection_error_px']<1 and r['residual_p90_px']<1.5 and r['ecc_score']>=.8 and r['inlier_ratio']>=.5)
    if not good:
        return state('NEEDS PROCESSING','المواءمة الهندسية وفحص الجودة مطلوبان','align')
    if comp['mode']=='STRUCTURAL COMPARISON':
        return state('READY','جاهز للمقارنة البنيوية فقط؛ النتائج تحتاج مراجعة','compare')
    if not radiometric:
        return state('NEEDS PROCESSING','فحص الإضاءة والألوان مطلوب','inspect_colors')
    if radiometric.get('recommend_match') and not radiometric_accepted:
        return state('NEEDS PROCESSING','يوصى بتوحيد الإضاءة والألوان؛ القرار للمستخدم','radiometric')
    return state('READY','الصورتان جاهزتان للمقارنة','compare')


def _read_analysis(ds,info,window=None,out_shape=None,intensity=False):
    indexes=info['rgb'] or info['non_alpha']
    data=ds.read(indexes,window=window,out_shape=((len(indexes),*out_shape) if out_shape else None),masked=True,
                 resampling=Resampling.nearest)
    raw=np.asarray(data,dtype=np.float32)
    valid=~np.any(np.ma.getmaskarray(data),axis=0) & np.all(np.isfinite(raw),axis=0) & np.any(raw!=0,axis=0)
    if intensity and info['rgb']:
        raw=(.2126*raw[0]+.7152*raw[1]+.0722*raw[2])[None]
    return raw,valid


def harmonize_pair(path_a,path_b,out_a,out_b,info_a,info_b,cancel=None,progress=None):
    comp=band_compatibility(info_a,info_b)
    if comp['mode']=='STRUCTURAL COMPARISON' and comp['status']=='NEEDS HARMONIZATION':
        from structural_comparison import prepare_pair
        return prepare_pair(path_a,path_b,out_a,out_b,info_a,info_b,cancel,progress)
    if comp['status']!='NEEDS HARMONIZATION':
        raise ValueError('توحيد الحزم غير مطلوب أو الحزم غير متوافقة.')
    inputs={os.path.normcase(os.path.realpath(p)) for p in (path_a,path_b)}
    if any(os.path.normcase(os.path.realpath(p)) in inputs or os.path.exists(p) for p in (out_a,out_b)) or out_a==out_b:
        raise ValueError('يجب استخدام ملفين جديدين مستقلين عن الأصول.')
    intensity=bool(info_a['rgb']) != bool(info_b['rgb'])
    staged=[]
    try:
        for position,(path,out,info) in enumerate(((path_a,out_a,info_a),(path_b,out_b,info_b))):
            check_cancel(cancel)
            with rasterio.open(path) as ds:
                s=min(1.,1024/max(ds.width,ds.height))
                sample,valid=_read_analysis(ds,info,out_shape=(max(1,round(ds.height*s)),max(1,round(ds.width*s))),intensity=intensity)
                if valid.sum()<32:
                    raise ValueError('عينات صالحة غير كافية لتوحيد الحزم.')
                ranges=[np.percentile(band[valid],(2,98)).astype(float) for band in sample]
                # Extraction alone preserves DN for already-8-bit bands. For
                # differing depths/ranges, use float32 percentile normalization
                # then quantize only for the existing uint8 comparison engine.
                normalize=intensity or comp.get('normalize') or any(dt!='uint8' for m in (info_a,info_b) for dt in m['dtypes']) or set(info_a['bit_depth'])!=set(info_b['bit_depth'])
                if normalize and any(hi<=lo for lo,hi in ranges):
                    raise ValueError('نطاق إشعاعي ثابت؛ لا يمكن تطبيع الحزمة بأمان.')
                Path(out).parent.mkdir(parents=True,exist_ok=True)
                fd,tmp=tempfile.mkstemp(suffix='.tif',dir=str(Path(out).parent));os.close(fd);staged.append((tmp,out))
                profile=lossless_profile(ds.profile,count=sample.shape[0],dtype='uint8',nodata=None,
                                         tiled=True,blockxsize=256,blockysize=256)
                from affine import Affine
                profile.update(crs=info['crs'],transform=Affine(*info['transform']))
                with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(tmp,'w',**profile) as dst:
                    for y in range(0,ds.height,512):
                        check_cancel(cancel)
                        for x in range(0,ds.width,512):
                            check_cancel(cancel)
                            win=Window(x,y,min(512,ds.width-x),min(512,ds.height-y))
                            raw,v=_read_analysis(ds,info,window=win,intensity=intensity)
                            result=np.zeros(raw.shape,np.uint8)
                            for k,(lo,hi) in enumerate(ranges):
                                vals=np.where(v,raw[k],lo)
                                vals=(vals-lo)/(hi-lo)*252+3 if normalize else vals
                                result[k]=np.clip(np.rint(vals),3 if normalize else 0,255).astype(np.uint8)
                                result[k,~v]=0
                            dst.write(result,window=win);dst.write_mask(v.astype(np.uint8)*255,window=win)
                        if progress:progress(int((position+(y+512)/ds.height)*45),'تجهيز نسخة التحليل دون تغيير المصدر...')
                    dst.colorinterp=(ColorInterp.gray,) if result.shape[0]==1 else (ColorInterp.red,ColorInterp.green,ColorInterp.blue)
                    dst.update_tags(ANALYSIS_SOURCE=str(Path(path).resolve()),ANALYSIS_MODE=comp['mode'],
                                    ANALYSIS_NORMALIZATION='P2_P98_FLOAT32_TO_UINT8' if normalize else 'BAND_SELECTION_ONLY')
                    for k in range(result.shape[0]):
                        dst.set_band_description(k+1,'Grayscale' if result.shape[0]==1 else ('Red','Green','Blue')[k])
                with rasterio.open(tmp) as check:
                    assert check.shape==ds.shape and check.crs==rasterio.crs.CRS.from_user_input(info['crs'])
                    for _,win in check.block_windows(1):
                        check_cancel(cancel);check.read(window=win)
        check_cancel(cancel)
        for tmp,out in staged:os.replace(tmp,out)
        return dict(outputs=[out_a,out_b],mode=comp['mode'],originals=[path_a,path_b])
    finally:
        for tmp,_ in staged:
            if os.path.exists(tmp):os.remove(tmp)


def copy_georef(path,output,info,cancel=None):
    """Embed explicitly recovered metadata into a new raster without resampling."""
    from affine import Affine
    if not info['crs_usable'] or not info['transform_usable'] or os.path.exists(output):
        raise ValueError('يلزم مرجع مكاني موثوق وملف جديد.')
    with rasterio.open(path) as src, rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
        profile=lossless_profile(src.profile,crs=info['crs'],transform=Affine(*info['transform']))
        with rasterio.open(output,'w',**profile) as dst:
            dst.colorinterp=src.colorinterp
            dst.update_tags(**src.tags())
            for i in range(1,src.count+1):
                if src.descriptions[i-1]:dst.set_band_description(i,src.descriptions[i-1])
                dst.update_tags(i,**src.tags(i))
            for _,win in dst.block_windows(1):
                check_cancel(cancel);dst.write(src.read(window=win),window=win)
                dst.write_mask(src.dataset_mask(window=win),window=win)
    return output


def crop_pair(a,b,oa,ob,cancel=None):
    import gis_tools
    inputs={os.path.realpath(p).casefold() for p in (a,b)}
    if any(os.path.realpath(p).casefold() in inputs for p in (oa,ob)):
        raise ValueError('لا يمكن القص فوق الصور الأصلية.')
    with tempfile.TemporaryDirectory(dir=str(Path(oa).parent)) as folder:
        ta,tb=str(Path(folder)/'before.tif'),str(Path(folder)/'after.tif')
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            check_cancel(cancel);result=gis_tools.crop_overlap(a,b,ta,tb);check_cancel(cancel)
        os.replace(ta,oa);os.replace(tb,ob)
        result.update(before_overlap=oa,after_overlap=ob)
        return result
