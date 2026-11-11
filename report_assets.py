"""Bounded report image decoding. Originals are always read-only."""
from __future__ import annotations
import hashlib
import io
import math
from pathlib import Path
from PIL import Image, ImageOps
from report_document import uid
from report_cancel import checkpoint

MAX_EDGE=2048
MAX_SOURCE_PIXELS=100_000_000
MAX_TOTAL_PIXELS=64_000_000

def decode_image(path, window=None, max_edge=MAX_EDGE):
    checkpoint('decode_start')
    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from rasterio.enums import Resampling
    path=Path(path);limit=min(MAX_EDGE,max(64,int(max_edge)))
    # Decoder/driver identify actual content, not the filename suffix.
    with path.open('rb') as stream:magic=stream.read(16)
    ordinary=(magic.startswith((b'\x89PNG',b'\xff\xd8',b'GIF87a',b'GIF89a',b'BM')) or (magic.startswith(b'RIFF') and magic[8:12]==b'WEBP'))
    # Palette GIF/PNG and EXIF/CMYK common images require color-aware Pillow
    # decoding. Geospatial/large rasters retain bounded Rasterio window reads.
    if ordinary:dataset=None
    else:
        try:dataset=rasterio.open(path)
        except rasterio.errors.RasterioIOError:dataset=None
    if dataset is not None:
        with dataset as src:
            w=Window(*window) if window is not None else Window(0,0,src.width,src.height)
            w=w.intersection(Window(0,0,src.width,src.height))
            if w.width<=0 or w.height<=0:raise ValueError('قصاصة خارج الصورة')
            scale=min(1,limit/max(w.width,w.height));width=max(1,int(w.width*scale));height=max(1,int(w.height*scale))
            if src.count<1:raise ValueError('الصورة بلا قنوات')
            color={getattr(c,'name',str(c)).lower():i+1 for i,c in enumerate(src.colorinterp)}
            indexes=[color[c] for c in ['red','green','blue']] if all(c in color for c in ['red','green','blue']) else list(range(1,min(3,src.count)+1))
            palette=len(indexes)==1 and 'palette' in color
            a=src.read(indexes,window=w,out_shape=(len(indexes),height,width),masked=True,resampling=Resampling.nearest if palette else Resampling.bilinear)
            checkpoint('decode_read')
            invalid=np.any(np.ma.getmaskarray(a),axis=0);raw=np.moveaxis(a.filled(0),0,-1)
            if palette:
                cmap=src.colormap(indexes[0]);lut=np.zeros((max(cmap)+1,3),np.uint8)
                for key,value in cmap.items():lut[key]=value[:3]
                rgb=lut[np.clip(raw[:,:,0].astype(np.int64),0,len(lut)-1)]
            elif raw.dtype!=np.uint8:
                rgb=np.zeros(raw.shape,dtype=np.uint8)
                for i in range(raw.shape[-1]):
                    good=(~invalid)&np.isfinite(raw[:,:,i]);vals=raw[:,:,i][good]
                    low,high=np.percentile(vals,[2,98]) if vals.size else (0,1)
                    rgb[:,:,i]=np.nan_to_num(np.clip((raw[:,:,i].astype(np.float32)-low)*255/max(float(high-low),1e-8),0,255)).astype(np.uint8)
            else:rgb=raw
            if rgb.shape[-1]<3:rgb=np.repeat(rgb[:,:,:1],3,axis=2)
            rgb[invalid]=0;image=Image.fromarray(rgb,'RGB')
            metadata={'decoder':'Rasterio/'+src.driver,'original_dimensions':[src.width,src.height],
                      'crs':str(src.crs) if src.crs else None,'source_transform':list(src.transform)[:6],
                      'crop_window':[w.col_off,w.row_off,w.width,w.height],
                      'display_conversion':'RGB uint8; P2/P98 if source depth requires it'}
    else:
        with Image.open(path) as source:
            if source.width*source.height>MAX_SOURCE_PIXELS:raise ValueError('الصورة أكبر من حد فك الذاكرة؛ استخدم قصاصة راستر')
            fmt=source.format;source.seek(0);image=ImageOps.exif_transpose(source).convert('RGB')
            if window is not None:
                x,y,w,h=window;image=image.crop((x,y,x+w,y+h))
            metadata={'decoder':'Pillow/'+str(fmt),'original_dimensions':list(source.size),'frame':0,'crs':None,'crop_window':window}
            image.thumbnail((limit,limit),Image.Resampling.LANCZOS)
    checkpoint('decode_png');output=io.BytesIO();image.save(output,format='PNG');checkpoint('decode_done')
    return output.getvalue(),{**metadata,'dimensions':list(image.size)}

class AssetStore:
    def __init__(self):self.payloads={};self.pixel_sizes={}
    def _reserve(self,payload,dimensions):
        if sum(map(len,self.payloads.values()))+len(payload)>512*1024*1024:raise ValueError('موارد التقرير تتجاوز 512MiB')
        if sum(self.pixel_sizes.values())+dimensions[0]*dimensions[1]>MAX_TOTAL_PIXELS:raise ValueError('تجاوز ميزانية بكسلات الصور؛ قلل حجم القصاصات')
    def ingest(self,path,window=None,max_edge=MAX_EDGE):
        payload,meta=decode_image(path,window,max_edge);key=uid()
        self._reserve(payload,meta['dimensions']);self.payloads[key]=payload;self.pixel_sizes[key]=meta['dimensions'][0]*meta['dimensions'][1]
        return {'uid':key,'format':'PNG','sha256':hashlib.sha256(payload).hexdigest(),
                'source_ref':str(Path(path).resolve()),'source_record':{'type':'external_readonly'},**meta}
    def add_png(self,payload,metadata=None):
        with Image.open(io.BytesIO(payload)) as image:
            if image.format!='PNG' or image.width*image.height>MAX_EDGE**2:raise ValueError('مورد PNG غير صالح أو كبير')
            dimensions=list(image.size);image.verify()
        self._reserve(payload,dimensions);key=uid();self.payloads[key]=bytes(payload);self.pixel_sizes[key]=dimensions[0]*dimensions[1]
        return {**(metadata or {}),'uid':key,'format':'PNG','sha256':hashlib.sha256(payload).hexdigest(),'dimensions':dimensions}
