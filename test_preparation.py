"""Preparation, data safety, workflow and viewport integration regression tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import main
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin, Affine
from PySide6.QtCore import QEventLoop,QTimer
import preparation as prep
import gis_tools


class PreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=main.QApplication.instance() or main.QApplication([])
        rng=np.random.default_rng(617)
        cls.rgb=rng.integers(10,220,(3,128,128),dtype=np.uint8)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()

    def write(self,name,data=None,crs='EPSG:32638',transform=None,descriptions=None,ci=None,nbits=None,nodata=None):
        data=self.rgb if data is None else data
        path=self.root/name
        with rasterio.open(path,'w',driver='GTiff',width=data.shape[2],height=data.shape[1],count=data.shape[0],
                           dtype=data.dtype,crs=crs,transform=transform or from_origin(500000,2800000,.5,.5),nodata=nodata) as ds:
            ds.write(data)
            if ci:ds.colorinterp=ci
            if descriptions:
                for i,value in enumerate(descriptions,1):ds.set_band_description(i,value)
            if nbits:ds.update_tags(NBITS=str(nbits))
        return str(path)

    def pair(self,a,b):return prep.inspect_raster(a),prep.inspect_raster(b)

    def test_metadata_classification_and_protected_semantics(self):
        rgb=self.write('rgb.tif')
        self.assertEqual(prep.inspect_raster(rgb)['band_type'],'RGB')
        for name,kind in [('panchromatic','Panchromatic'),('NIR','NIR'),('elevation DEM','DEM')]:
            p=self.write(kind+'.tif',self.rgb[:1],descriptions=[name])
            self.assertEqual(prep.inspect_raster(p)['band_type'],kind)
        values=np.concatenate([self.rgb,self.rgb[:1]])
        p=self.write('multi.tif',values,descriptions=['Blue','Green','Red','NIR'])
        info=prep.inspect_raster(p)
        self.assertEqual(info['rgb'],[3,2,1]);self.assertEqual(info['band_type'],'Multispectral')
        with self.assertRaises(ValueError):prep.inspect_raster(p,{'rgb':[4,2,1]})
        p=self.write('unknown.tif',values.astype(np.uint16),ci=(ColorInterp.undefined,)*4)
        info=prep.inspect_raster(p);self.assertEqual(info['band_type'],'Unknown')
        declared=prep.inspect_raster(p,{'rgb':[3,2,1]})
        self.assertTrue(declared['user_declared']);self.assertEqual(declared['rgb'],[3,2,1])
        self.assertEqual(prep.inspect_raster(p)['band_type'],'Unknown')
        self.assertEqual(prep._semantic('',{'wavelength':'850'}),'Unknown')
        self.assertEqual(prep._semantic('',{'wavelength':'850','wavelength_units':'nm'}),'NIR')
        unnamed=self.write('unnamed.tif',self.rgb[:1])
        self.assertEqual(prep.inspect_raster(unnamed)['band_type'],'Unknown')
        self.assertEqual(prep.inspect_raster(unnamed,{'type':'NIR'})['band_type'],'NIR')

    def test_compatibility_matrix(self):
        rgb=self.write('rgb.tif');gray=self.write('gray.tif',self.rgb[:1],descriptions=['Grayscale'])
        pan=self.write('pan.tif',self.rgb[:1],descriptions=['Panchromatic'])
        nir=self.write('nir.tif',self.rgb[:1],descriptions=['NIR'])
        dem=self.write('dem.tif',self.rgb[:1].astype(np.float32),descriptions=['DEM'])
        for a,b,status in [(rgb,rgb,'READY'),(gray,gray,'READY'),(pan,pan,'READY'),
                            (rgb,gray,'NEEDS HARMONIZATION'),(rgb,pan,'NEEDS HARMONIZATION'),
                            (rgb,nir,'NEEDS HARMONIZATION'),(rgb,dem,'NOT READY')]:
            with self.subTest(a=a,b=b):self.assertEqual(prep.band_compatibility(*self.pair(a,b))['status'],status)

    def test_missing_crs_grid_overlap_rotation_and_sidecar(self):
        a=self.write('a.tif');missing=self.write('missing.tif',crs=None)
        s=prep.readiness(*self.pair(a,missing));self.assertEqual(s['status'],'NOT READY');self.assertIn('CRS',s['reason'])
        shifted=self.write('shifted.tif',transform=from_origin(500010,2800000,.5,.5))
        s=prep.readiness(*self.pair(a,shifted));self.assertEqual(s['action'],'grid')
        self.assertAlmostEqual(s['overlap']['percent_a'],84.375)
        partial=self.write('partial.tif',transform=from_origin(500040,2800000,.5,.5))
        self.assertEqual(prep.readiness(*self.pair(a,partial))['action'],'crop')
        outside=self.write('outside.tif',transform=from_origin(501000,2800000,.5,.5))
        self.assertEqual(prep.readiness(*self.pair(a,outside))['status'],'NOT READY')
        skew=self.write('skew.tif',transform=Affine(.5,.1,500000,.2,-.5,2800000))
        info=prep.inspect_raster(skew);self.assertAlmostEqual(info['pixel_size'][0],np.hypot(.5,.2))
        self.assertTrue(info['transform_usable']);self.assertEqual(info['rotation_skew'],[.1,.2])
        Path(missing).with_suffix('.prj').write_text(rasterio.crs.CRS.from_epsg(32638).to_wkt(),encoding='utf-8')
        info=prep.inspect_raster(missing);self.assertTrue(info['crs_usable'])
        self.assertTrue(info['recovered_georef'])
        out=str(self.root/'metadata_copy.tif');prep.copy_georef(missing,out,info)
        with rasterio.open(missing) as ds:self.assertIsNone(ds.crs)
        with rasterio.open(out) as ds:self.assertEqual(ds.crs.to_epsg(),32638)

    def test_grid_retains_band_metadata(self):
        ref=self.write('ref.tif')
        source=self.write('multi.tif',np.concatenate([self.rgb,self.rgb[:1]]),descriptions=['Blue','Green','Red','NIR'])
        out=str(self.root/'grid.tif')
        gis_tools.align_to_reference(ref,source,out)
        info=prep.inspect_raster(out)
        self.assertEqual(info['rgb'],[3,2,1]);self.assertEqual(info['band_types'][3],'NIR')

    def test_harmonization_bit_depths_intensity_mapping_and_nodata(self):
        for bits in (12,16):
            with self.subTest(bits=bits):
                a=self.write('rgb8_'+str(bits)+'.tif')
                factor=12 if bits==12 else 180
                raw=self.rgb[:1].astype(np.uint16)*factor
                raw[:,:8]=65535
                b=self.write('pan'+str(bits)+'.tif',raw,descriptions=['Panchromatic'],nbits=bits,nodata=65535)
                hashes=[hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (a,b)]
                ia,ib=self.pair(a,b);self.assertEqual(ib['bit_depth'],[bits])
                oa,ob=str(self.root/f'intensity{bits}.tif'),str(self.root/f'pan_ready{bits}.tif')
                prep.harmonize_pair(a,b,oa,ob,ia,ib)
                self.assertEqual(prep.band_compatibility(*self.pair(oa,ob))['status'],'READY')
                for p in (oa,ob):
                    with rasterio.open(p) as ds:
                        self.assertEqual(ds.count,1);self.assertEqual(ds.dtypes,('uint8',))
                        self.assertEqual(ds.transform,from_origin(500000,2800000,.5,.5))
                        self.assertEqual(ds.shape,(128,128))
                with rasterio.open(ob) as ds:self.assertFalse(ds.dataset_mask()[:8].any())
                self.assertEqual(hashes,[hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (a,b)])
        a=self.write('multi.tif',np.concatenate([self.rgb,self.rgb[:1]]),descriptions=['Blue','Green','Red','NIR'])
        b=self.write('rgbmap.tif',self.rgb[[2,1,0]])
        oa,ob=str(self.root/'mapped.tif'),str(self.root/'mapped2.tif')
        prep.harmonize_pair(a,b,oa,ob,*self.pair(a,b))
        with rasterio.open(oa) as ds:np.testing.assert_array_equal(ds.read(),self.rgb[[2,1,0]])

    def test_cancellation_never_replaces_sources_or_publishes_harmonization(self):
        a=self.write('a.tif');b=self.write('b.tif',self.rgb[:1],descriptions=['Panchromatic'])
        with self.assertRaises(ValueError):prep.harmonize_pair(a,b,a,str(self.root/'bad.tif'),*self.pair(a,b))
        with self.assertRaises(prep.Cancelled):
            prep.harmonize_pair(a,b,str(self.root/'cancel1.tif'),str(self.root/'cancel2.tif'),*self.pair(a,b),cancel=lambda:True)
        self.assertFalse((self.root/'cancel1.tif').exists())
        before=Path(a).read_bytes()
        with self.assertRaises(ValueError):gis_tools.radiometric_match(a,a,a)
        with self.assertRaises(prep.Cancelled):gis_tools.align_to_reference(a,a,str(self.root/'grid.tif'),cancel=lambda:True)
        self.assertEqual(before,Path(a).read_bytes())

    def test_single_band_12_to_16_and_16_to_16(self):
        for bits in (12,16):
            a=self.write(f'pan{bits}.tif',self.rgb[:1].astype(np.uint16)*10,descriptions=['Panchromatic'],nbits=bits)
            b=self.write(f'pan16_{bits}.tif',self.rgb[:1].astype(np.uint16)*200,descriptions=['Panchromatic'],nbits=16)
            oa,ob=str(self.root/f'analysis_a{bits}.tif'),str(self.root/f'analysis_b{bits}.tif')
            prep.harmonize_pair(a,b,oa,ob,*self.pair(a,b))
            with rasterio.open(oa) as x,rasterio.open(ob) as y:
                np.testing.assert_array_equal(x.read(),y.read())

    def test_gray_mesh_radiometric_and_geographic_sync(self):
        import radiometric_inspection
        a=self.write('gray1.tif',self.rgb[:1]);b=self.write('gray2.tif',self.rgb[:1])
        aligned=np.repeat(self.rgb[0,:,:,None],3,axis=2)
        result=radiometric_inspection.inspect_radiometry(a,b,alignment={
            'report':{'alignment_approved':True,'smooth_mesh_passes':1},'aligned':aligned,
            'valid_mask':np.full((128,128),255,np.uint8)})
        self.assertFalse(result['recommend_match']);self.assertEqual(len(result['bands']),1)
        with patch.object(Path,'home',return_value=self.root),patch.object(main.MainWindow,'_cleanup_stale_temp_files'),patch.object(main.MainWindow,'_save_report'):
            window=main.MainWindow();self.addCleanup(window.close)
            window._load_a(a)
            shifted=self.write('shifted.tif',self.rgb[:1],transform=from_origin(500010,2800000,.5,.5))
            window._load_b(shifted)
            point=window._workflow_map_point(window.va,window.vb,60,50)
            self.assertAlmostEqual(point[0],40);self.assertAlmostEqual(point[1],50)
            window.close()

    def test_readiness_quality_and_radiometric_decision(self):
        a=self.write('a.tif');ia,ib=self.pair(a,a)
        self.assertEqual(prep.readiness(ia,ib)['action'],'align')
        report=dict(alignment_approved=True,reprojection_error_px=.2,residual_p90_px=.4,ecc_score=.95,inlier_ratio=.9)
        self.assertEqual(prep.readiness(ia,ib,report)['action'],'inspect_colors')
        self.assertEqual(prep.readiness(ia,ib,report,{'recommend_match':True})['action'],'radiometric')
        self.assertEqual(prep.readiness(ia,ib,report,{'recommend_match':True},True)['status'],'READY')
        report['residual_p90_px']=4
        self.assertNotEqual(prep.readiness(ia,ib,report,{'recommend_match':False})['status'],'READY')

    def test_ui_sync_full_view_selection_undo_harmonization_and_project(self):
        a=self.write('a.tif');b=self.write('b.tif',self.rgb[:1],descriptions=['Panchromatic'])
        with patch.object(Path,'home',return_value=self.root),patch.object(main.MainWindow,'_cleanup_stale_temp_files'),\
                patch.object(main.MainWindow,'_save_report'),patch.object(main.MainWindow,'err') as errors:
            window=main.MainWindow();window.resize(1800,1000);window.show();self.app.processEvents()
            self.addCleanup(window.close)
            window._load_a(a);window._load_b(b);self.app.processEvents()
            self.assertEqual(window._wf_state['action'],'harmonize');self.assertTrue(window.harmonize_button.isEnabled())
            with patch.object(window,'_new_analysis_path',side_effect=lambda name:str(self.root/(name+'.tif'))):
                window.workflow_harmonize()
                loop=QEventLoop();done=[]
                worker=window._wf_task;worker.finished.connect(lambda:done.append(True));worker.finished.connect(loop.quit)
                QTimer.singleShot(15000,loop.quit);loop.exec();self.app.processEvents()
                self.assertTrue(done)
            errors.assert_not_called()
            self.assertEqual(window._wf_state['action'],'align')
            self.assertEqual(window._wf_originals,[a,b])
            window.workflow_undo();self.assertEqual(window.a.path,a);self.assertEqual(window.b.path,b)
            self.assertEqual(window._wf_state['action'],'harmonize')
            window._load_b(a);window.vr.set_array(window.a.array);self.app.processEvents()
            window.va.apply_view_state(dict(cx=60,cy=50,scene_scale=3))
            window.va._emit_view_state();self.app.processEvents()
            for view in (window.vb,window.vr):
                state=view._view_state();self.assertAlmostEqual(state['scene_scale'],3,places=6)
                self.assertAlmostEqual(state['cx'],window.va._view_state()['cx'],delta=1)
            window.workflow_full_view(window.after_box)
            self.assertTrue(window.before_box.isHidden());self.assertFalse(window.after_box.isHidden())
            window.workflow_full_view(window.after_box)
            self.assertFalse(window.before_box.isHidden());self.assertFalse(window.change_box.isHidden())
            self.assertTrue(window.workflow_focus_region(dict(id=1,x=20,y=20,width=10,height=10,cx=25,cy=25)))
            self.assertEqual(len(window._wf_markers),9)
            project=str(self.root/'project.cvp')
            window._wf_declarations[a]={'rgb':[1,2,3]}
            with patch.object(main.QFileDialog,'getSaveFileName',return_value=(project,'')), patch.object(main.QInputDialog,'getItem',return_value=('حفظ المراجع الخارجية فقط (ملف أصغر)',True)):window.save_project()
            payload=main.read_project(project)
            self.assertEqual(payload['preparation']['originals'],[a,a])
            with patch.object(main.QFileDialog,'getOpenFileName',return_value=(project,'')):window.open_project()
            self.assertEqual(window._wf_declarations[a],{'rgb':[1,2,3]})
            errors.assert_not_called();window.close()


if __name__=='__main__':unittest.main(verbosity=2)
