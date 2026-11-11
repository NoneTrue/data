import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from PIL import Image
from report_adapter import from_snapshot
from report_document import ReportDocument,ReportController,block
from report_assets import AssetStore,decode_image
from report_project import save_project,load_project

class ReportProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.image=self.root/'original.png';Image.new('RGB',(64,32),'orange').save(self.image)
    def pair(self,count=7):
        snapshot={'image1':{'crs':'EPSG:4326'},'change_summary':{'changed_area_map_units2':0.0},
          'report_metadata':{'operation_name':'مهمة اصطناعية'},'regions':[
          {'id':i,'target_uid':f'uid-{i}','target_name':'','report_included':True,
           'longitude':49.1234567890123,'report_images':[]} for i in range(count)]}
        if count:
            for i in range(5):snapshot['regions'][i%count]['report_images'].append({'path':str(self.image),'source':'after','report_order':i})
        return snapshot,from_snapshot(snapshot)
    def test_snapshot_identity_independence_and_five_placements(self):
        snap,(d,a)=self.pair();before=copy.deepcopy(snap)
        data=d.to_dict();self.assertEqual(len(data['targets']),7)
        images=[b for s in data['sections'] for b in s['blocks'] if b['kind']=='image']
        self.assertEqual(len(images),5);self.assertEqual(len({b['placement_uid'] for b in images}),5)
        self.assertEqual([b['target_uid'] for b in images],[f'uid-{i}' for i in range(5)])
        self.assertEqual(snap,before);data['analysis_snapshot'].clear();self.assertEqual(d.analysis_snapshot,before)
        metric=next(s for s in data['sections'] if s['kind']=='metrics')['blocks'][0]
        self.assertEqual(metric['value'],0);self.assertIn('ليست m²',metric['unit'])
    def test_duplicate_uid_is_rejected(self):
        snap,_=self.pair();snap['regions'][1]['target_uid']='uid-0'
        with self.assertRaises(ValueError):from_snapshot(snap)
    def test_zero_targets_and_missing_value(self):
        _,(d,a)=self.pair(0);path=self.root/'empty.cvreport';save_project(path,d,a)
        reopened,_=load_project(path);self.assertEqual(reopened.to_dict(),d.to_dict())
    def test_round_trip_portable_with_annotations_hidden_and_custom_table(self):
        _,(d,a)=self.pair();c=ReportController(d);sid=c.add_section('قسم مخصص')
        c.add_block(sid,block('paragraph',text='تعليق محلل عربي',style={'font':'Arial','size':18,'color':'#123456'}))
        c.add_block(sid,block('table',columns=[{'key':'k','title':'حقل','visible':False,'width':3}],rows=[['قيمة']],locked=False))
        c.mutate(lambda x:x['sections'][0].update(visible=False))
        def annotations(x):
            item=next(b for s in x['sections'] for b in s['blocks'] if b['kind']=='image')
            item['annotations']=[{'id':'circle','type':'circle','points':[[1,1],[11,11]],'size':4,'report_schema':1,'color':'#123456'},
                                 {'id':'text','type':'text','points':[[2,15]],'text':'نص عربي','font_family':'Arial','font_size':22,'color':'#ff0000','report_schema':1}]
        c.mutate(annotations)
        path=self.root/'portable.cvreport';save_project(path,c.document,a);self.image.unlink()
        reopened,store=load_project(path);self.assertEqual(reopened.to_dict(),c.document.to_dict());self.assertEqual(store.payloads,a.payloads)
    def test_reopen_in_new_python_process(self):
        _,(d,a)=self.pair();p=self.root/'restart.cvreport';save_project(p,d,a)
        code='import sys;from report_project import load_project;d,a=load_project(sys.argv[1]);assert len(d.to_dict()["targets"])==7;assert len(a.payloads)==5;print(d.to_dict()["report_uid"])'
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',code,str(p)],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(),d.to_dict()['report_uid'])
    def test_failed_commit_keeps_previous_and_cleans_temporary(self):
        _,(d,a)=self.pair();p=self.root/'safe.cvreport';save_project(p,d,a);before=p.read_bytes()
        with patch('report_project.os.replace',side_effect=OSError('simulated disk/permission failure')):
            with self.assertRaises(OSError):save_project(p,d,a,overwrite=True)
        self.assertEqual(p.read_bytes(),before);self.assertEqual(list(self.root.glob('*.tmp')),[])
        with self.assertRaises(FileExistsError):save_project(p,d,a)
    def test_unknown_version_and_path_traversal_rejected(self):
        _,(d,a)=self.pair();p=self.root/'bad.cvreport';save_project(p,d,a)
        with zipfile.ZipFile(p,'a') as z:z.writestr('../escape',b'bad')
        with self.assertRaises(ValueError):load_project(p)
        data=d.to_dict();data['schema_version']=999
        with self.assertRaises(ValueError):ReportDocument(data)
        self.assertFalse((self.root.parent/'escape').exists())
    def test_corrupt_payload_rejected(self):
        _,(d,a)=self.pair();p=self.root/'corrupt.cvreport';save_project(p,d,a)
        with zipfile.ZipFile(p) as z:parts={n:z.read(n) for n in z.namelist()}
        asset=next(n for n in parts if n.startswith('assets/'));parts[asset]=b'corrupt'
        with zipfile.ZipFile(p,'w') as z:
            for n,b in parts.items():z.writestr(n,b)
        with self.assertRaises(ValueError):load_project(p)
    def test_commands_undo_protect_analysis_and_restore_visibility(self):
        _,(d,a)=self.pair();c=ReportController(d);original=d.to_dict()
        locked=next(b for s in original['sections'] for b in s['blocks'] if b.get('locked'))
        with self.assertRaises(ValueError):c.edit_block(locked['uid'],{'value':'tampered'})
        with self.assertRaises(ValueError):c.mutate(lambda x:x['analysis_snapshot'].clear())
        c.mutate(lambda x:x['sections'][0].update(visible=False));self.assertTrue(c.dirty)
        c.undo();self.assertEqual(c.document.to_dict(),original);self.assertFalse(c.dirty)
        c.redo();self.assertFalse(c.document.to_dict()['sections'][0]['visible']);c.restore_sections()
        self.assertTrue(all(s['visible'] for s in c.document.to_dict()['sections']))
    def test_format_content_detection_and_original_unchanged(self):
        for fmt in ['PNG','JPEG','BMP','WEBP','TIFF','GIF']:
            with self.subTest(fmt=fmt):
                p=self.root/(fmt+'.wrong_extension');Image.new('RGB',(40,20),'red').save(p,format=fmt)
                before=hashlib.sha256(p.read_bytes()).hexdigest();payload,meta=decode_image(p)
                with Image.open(io.BytesIO(payload)) as im:
                    self.assertEqual(im.size,(40,20));pixel=im.convert('RGB').getpixel((10,10));self.assertGreater(pixel[0],245);self.assertLess(pixel[1],6);self.assertLess(pixel[2],6)
                self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),before)
    def test_geotiff_window_and_depth_bounded_preview(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        p=self.root/'large.tif'
        with rasterio.open(p,'w',driver='GTiff',width=6000,height=5000,count=3,dtype='uint16',crs='EPSG:32638',transform=from_origin(500000,2800000,2,2),tiled=True,compress='deflate') as ds:
            ds.write(np.ones((3,256,256),np.uint16)*1000,window=rasterio.windows.Window(128,128,256,256))
        payload,meta=decode_image(p,window=[128,128,256,256],max_edge=128)
        self.assertEqual(meta['dimensions'],[128,128]);self.assertEqual(meta['crs'],'EPSG:32638')
        self.assertEqual(meta['crop_window'],[128,128,256,256])
    def test_actual_jpeg2000_decoder(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        with rasterio.Env() as env:
            if 'JP2OpenJPEG' not in env.drivers():self.skipTest('JP2OpenJPEG unavailable; JP2 not claimed')
        p=self.root/'sample.jp2'
        with rasterio.open(p,'w',driver='JP2OpenJPEG',width=32,height=24,count=3,dtype='uint8',crs='EPSG:4326',transform=from_origin(49,12,.01,.01)) as ds:ds.write(np.full((3,24,32),100,np.uint8))
        payload,meta=decode_image(p);self.assertEqual(meta['dimensions'],[32,24]);self.assertIn('JP2',meta['decoder'])
    def test_palette_gif_first_frame_preserves_actual_color(self):
        p=self.root/'frames.disguised';Image.new('RGB',(20,10),'red').save(p,format='GIF',save_all=True,append_images=[Image.new('RGB',(20,10),'blue')])
        payload,meta=decode_image(p)
        with Image.open(io.BytesIO(payload)) as im:self.assertEqual(im.convert('RGB').getpixel((5,5)),(255,0,0))
        self.assertEqual(meta['frame'],0)
    def test_numpy_snapshot_metadata_is_copied_without_scalar_item_failure(self):
        import numpy as np
        array=np.array([[1.,2.],[3.,4.]])
        doc,assets=from_snapshot({'change_summary':{'small_array':array,'count':np.int64(0)}})
        self.assertEqual(doc.analysis_snapshot['change_summary']['small_array'],[[1.,2.],[3.,4.]])
        self.assertEqual(doc.analysis_snapshot['change_summary']['count'],0);self.assertTrue(np.array_equal(array,[[1,2],[3,4]]))
    def test_palette_geotiff_and_declared_rgb_band_order(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        from rasterio.enums import ColorInterp
        palette=self.root/'palette.tif'
        with rasterio.open(palette,'w',driver='GTiff',width=4,height=4,count=1,dtype='uint8',crs='EPSG:4326',transform=from_origin(49,12,.01,.01)) as ds:
            ds.write(np.zeros((1,4,4),np.uint8));ds.write_colormap(1,{0:(255,0,0,255),1:(0,0,255,255)})
        payload,_=decode_image(palette)
        with Image.open(io.BytesIO(payload)) as im:self.assertEqual(im.getpixel((0,0)),(255,0,0))
        bands=self.root/'bands.tif'
        with rasterio.open(bands,'w',driver='GTiff',width=4,height=4,count=3,dtype='uint8',crs='EPSG:4326',transform=from_origin(49,12,.01,.01)) as ds:
            ds.write(np.stack([np.full((4,4),20,np.uint8),np.full((4,4),30,np.uint8),np.full((4,4),240,np.uint8)]));ds.colorinterp=(ColorInterp.blue,ColorInterp.green,ColorInterp.red)
        payload,_=decode_image(bands)
        with Image.open(io.BytesIO(payload)) as im:self.assertEqual(im.getpixel((0,0)),(240,30,20))

if __name__=='__main__':unittest.main()
