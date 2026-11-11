"""Cross-spectral safety and guided processing integration."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import main
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import cv2
import numpy as np
import rasterio
from rasterio.transform import from_origin
import preparation
import workflow_ui
import structural_comparison as structural
from PySide6.QtCore import QEventLoop,QTimer


class StructuralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='.')
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        rng=np.random.default_rng(71)
        self.gray=cv2.GaussianBlur(rng.uniform(20,210,(384,384)).astype(np.float32),(0,0),1.2)
        for _ in range(90):
            x,y=rng.integers(25,340,2);cv2.rectangle(self.gray,(int(x),int(y)),(int(x+12),int(y+12)),float(rng.uniform(20,210)),-1)
        self.valid=np.ones(self.gray.shape,bool);self.valid[:8]=False

    def write(self,name,data,nir=False):
        path=str(self.root/name)
        with rasterio.open(path,'w',driver='GTiff',width=384,height=384,count=len(data),dtype='float32',
                           transform=from_origin(500000,2800000,.5,.5),crs='EPSG:32638') as ds:
            ds.write(data.astype(np.float32));ds.write_mask(self.valid.astype(np.uint8)*255)
            if nir:ds.set_band_description(1,'NIR')
            else:ds.colorinterp=(rasterio.enums.ColorInterp.red,rasterio.enums.ColorInterp.green,rasterio.enums.ColorInterp.blue)
        return path

    def pair(self):
        a=self.write('rgb.tif',np.stack([self.gray]*3))
        b=self.write('nir.tif',(900-3*self.gray)[None],True)
        return a,b

    def test_polarity_gain_and_new_structure(self):
        a,va=structural.descriptors(self.gray,self.valid)
        b,vb=structural.descriptors(900-3*self.gray,self.valid)
        with patch.object(main.engine,'radiometric_match',side_effect=AssertionError('Ordinary radiometric forbidden')),\
             patch.object(main.engine,'absolute_difference',side_effect=AssertionError('Raw pixels forbidden')):
            d,meta=main.engine.compare(a,b,'STRUCTURAL',va&vb)
        self.assertLess(float(d[va&vb].mean()),1)
        changed=self.gray.copy();changed[145:205,145:205]=100
        c,vc=structural.descriptors(changed,self.valid)
        dc,_=main.engine.compare(a,c,'STRUCTURAL',va&vc)
        self.assertGreater(float(dc[145:205,145:205].mean()),10)
        self.assertFalse(np.any(dc[~va]));self.assertFalse(meta['raw_pixel_difference'])

    def test_geotiff_features_and_quality_gate_and_cancel(self):
        a,b=self.pair();infos=[preparation.inspect_raster(p) for p in (a,b)]
        original=[Path(p).read_bytes() for p in (a,b)]
        oa,ob=[str(self.root/p) for p in ('sa.tif','sb.tif')]
        result=preparation.harmonize_pair(a,b,oa,ob,*infos)
        self.assertEqual(result['mode'],structural.MODE)
        import gis_tools, radiometric_inspection
        with self.assertRaises(ValueError):gis_tools.radiometric_match(oa,ob,str(self.root/'forbidden.tif'))
        with self.assertRaises(RuntimeError):radiometric_inspection.inspect_radiometry(oa,ob)
        prepared=[preparation.inspect_raster(p) for p in (oa,ob)]
        self.assertEqual(prepared[0]['band_type'],'Structural')
        self.assertEqual(preparation.readiness(*prepared)['action'],'align')
        report=dict(alignment_approved=True,reprojection_error_px=.3,residual_p90_px=.6,ecc_score=.94,inlier_ratio=.9)
        self.assertEqual(preparation.readiness(*prepared,report)['status'],'READY')
        report['residual_p90_px']=2
        self.assertNotEqual(preparation.readiness(*prepared,report)['status'],'READY')
        for i,p in enumerate((oa,ob)):
            with rasterio.open(p) as ds:
                self.assertEqual(list(ds.transform)[:6],infos[i]['transform'])
                self.assertEqual(ds.crs.to_string(),infos[i]['crs'])
                self.assertEqual(ds.shape,(384,384));self.assertGreater(np.sum(ds.dataset_mask()==0),0)
            self.assertIsNotNone(cv2.imread(p,cv2.IMREAD_UNCHANGED))
        self.assertEqual(original,[Path(p).read_bytes() for p in (a,b)])
        with self.assertRaises(preparation.Cancelled):
            preparation.harmonize_pair(a,b,str(self.root/'cancel_a.tif'),str(self.root/'cancel_b.tif'),*infos,cancel=lambda:True)
        self.assertFalse((self.root/'cancel_a.tif').exists())

    def test_real_alignment_and_change_worker(self):
        a,valid=structural.descriptors(self.gray,self.valid)
        b,_=structural.descriptors(900-3*self.gray,self.valid)
        b=cv2.warpAffine(b,np.float32([[1,0,3],[0,1,-2]]),(384,384))
        output=[];errors=[]
        worker=main.AlignWorker(a,b,0.75)
        worker.completed.connect(lambda *r:output.append(r));worker.failed.connect(errors.append)
        worker.run();self.assertFalse(errors,errors);self.assertTrue(output)
        aligned,_,report,mask=output[0]
        self.assertTrue(report['alignment_approved']);self.assertLess(report['residual_p90_px'],1.5)
        self.assertLess(report['reprojection_error_px'],1)
        print('STRUCTURAL ALIGNMENT:',{k:report[k] for k in ('matches','inlier_ratio','reprojection_error_px','residual_p90_px','ecc_score','smooth_mesh_passes')})
        changed=self.gray.copy();changed[145:205,145:205]=100
        c,vc=structural.descriptors(changed,self.valid)
        diff,_=main.engine.compare(a,c,'STRUCTURAL',valid&vc)
        results=[]
        data=main.engine.ImageData('synthetic',a,None,from_origin(500000,2800000,.5,.5),'EPSG:32638')
        change=main.ChangeWorker(diff,15,10,3,valid&vc,data,method='STRUCTURAL')
        change.completed.connect(lambda *args:results.append(args));change.failed.connect(errors.append)
        with patch.object(main.engine,'radiometric_match',side_effect=AssertionError('Forbidden')):
            change.run()
        self.assertFalse(errors,errors);self.assertTrue(results)
        mask,regions,stats=results[0];self.assertGreater(len(regions),0)
        self.assertEqual(stats['comparison_mode'],'STRUCTURAL')
        self.assertEqual(stats['verification_review_count'],len(regions))
        self.assertFalse(np.any(mask[~vc]))

    def test_ui_sequence_only_needed_stages_and_structural_guards(self):
        a,b=self.pair()
        with patch.object(Path,'home',return_value=self.root),patch.object(main.MainWindow,'_cleanup_stale_temp_files'),\
             patch.object(main.MainWindow,'_save_report'),patch.object(main.MainWindow,'err') as errors:
            w=main.MainWindow();self.addCleanup(w.close);w._load_a(a);w._load_b(b)
            self.assertEqual(w.method.currentText(),'STRUCTURAL');self.assertFalse(w.method.isEnabled())
            self.assertFalse(w.gis_radiometric_button.isEnabled())
            with self.assertRaises(RuntimeError):w._workflow_radiometric_gate()
            self.assertTrue(all(not c.isEnabled() for c in w._comparison_controls))
            # Exercise real background preparation, with bounded geometry stub to
            # isolate orchestration; real geometry is tested separately above.
            def approve():
                w.last_alignment_report=dict(alignment_approved=True,reprojection_error_px=.2,residual_p90_px=.3,ecc_score=.95,inlier_ratio=.95)
                w._refresh_readiness()
            with patch.object(w,'_new_analysis_path',side_effect=lambda name:str(self.root/(name+'.tif'))),\
                 patch.object(w,'align',side_effect=approve) as align,patch.object(w,'workflow_radiometric') as rad,\
                 patch.object(w,'gis_inspect_radiometry') as inspect:
                w.workflow_sequence_start()
                loop=QEventLoop();timer=QTimer();timer.timeout.connect(lambda:loop.quit() if not w._wf_sequence and not w._workflow_busy() else None)
                timer.start(10);QTimer.singleShot(15000,loop.quit);loop.exec();timer.stop();self.app.processEvents()
                align.assert_called_once();rad.assert_not_called();inspect.assert_not_called()
                self.assertEqual(w._wf_state['status'],'READY');self.assertFalse(w._wf_sequence)
                self.assertTrue(all(c.isEnabled() for c in w._comparison_controls))
            errors.assert_not_called()
            w._wf_radiometric={'recommend_match':True};w._wf_sequence=True
            with patch.object(w,'_refresh_readiness'),patch.object(w,'workflow_radiometric') as rad:
                w._wf_state=dict(action='radiometric',reason='user decision')
                w._workflow_sequence_continue();rad.assert_not_called();self.assertFalse(w._wf_sequence)
            w.close()

    def test_result_review_exclusion_and_undo(self):
        a,b=self.pair()
        with patch.object(Path,'home',return_value=self.root),patch.object(main.MainWindow,'_cleanup_stale_temp_files'),\
             patch.object(main.MainWindow,'_save_report'),patch.object(main.MainWindow,'err') as errors:
            w=main.MainWindow();self.addCleanup(w.close);w._load_a(a);w._load_b(b)
            w.mask=np.zeros((384,384),np.uint8);w.mask[50:70,50:70]=255;w.mask[100:120,100:120]=255
            w.diff=w.mask.copy();w.regions=main.engine.region_table(w.mask,w.diff,w.a);w._fill_regions()
            w.table.selectRow(0)
            with patch.object(workflow_ui.QInputDialog,'getItem',return_value=('تمت المراجعة — مقبول',True)):
                w.workflow_review_result()
            self.assertEqual(w.regions[0]['review_status'],'تمت المراجعة — مقبول')
            before=w.mask.copy();w.workflow_delete_result()
            self.assertEqual(len(w.regions),1);self.assertEqual(np.count_nonzero(w.mask),400)
            w.workflow_undo();self.assertTrue(np.array_equal(w.mask,before));self.assertEqual(len(w.regions),2)
            errors.assert_not_called();w.close()


if __name__=='__main__':unittest.main()
