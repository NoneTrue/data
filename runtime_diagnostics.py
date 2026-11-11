"""Inventory plus real GDAL/PROJ/Qt/OTB smoke checks; never loads weights."""
import ast, importlib.util, json, os, re, subprocess, tempfile, sys, time
from pathlib import Path
import runtime_paths as paths


def scan_project(root):
    root=Path(root);hits=[];inventory=[];imports={}
    pattern=re.compile(r'(?i)(?:\b[A-Z]:[\\/]|Program Files|GDAL_DATA|PROJ_(?:LIB|DATA)|OTB_APPLICATION_PATH)')
    extensions={'.py','.bat','.cmd','.ps1','.spec','.iss','.json','.xml','.yaml','.yml','.txt','.md','.qss','.ini','.cfg','.cmake','.c','.cpp','.h'}
    for directory,folders,files in os.walk(root):
        folders[:]=[n for n in folders if n not in ('.git','__pycache__','dist','build','.venv') and not n.startswith('tmp')]
        for name in files:
            p=Path(directory)/name;rel=p.relative_to(root).as_posix();suffix=p.suffix.lower()
            category=('TRAINING_OR_DEVELOPMENT_ONLY' if rel.startswith(('backups/','validation/')) or name.startswith('test_')
                      else 'OPTIONAL_EXTERNAL' if rel.startswith('runtime/') else 'APPLICATION')
            if suffix in ('.pt','.pth','.onnx','.names','.ico','.png','.exe','.dll','.yaml','.yml') or name=='classes.txt':
                inventory.append(dict(path=rel,bytes=p.stat().st_size,category=category))
            if suffix not in extensions or p.stat().st_size>8*1024*1024:continue
            try:text=p.read_text(encoding='utf-8-sig',errors='replace')
            except OSError:continue
            for i,line in enumerate(text.splitlines(),1):
                if pattern.search(line):hits.append(dict(path=rel,line=i,text=line[:500],category=category))
            if suffix=='.py' and p.parent==root:
                try:
                    tree=ast.parse(text)
                    imports[rel]=sorted({n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}|
                                        {a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names})
                except SyntaxError:pass
    return dict(path_hits=hits,resources=inventory,imports=imports)


def run(output,smoke=False,scan=False):
    output=Path(output);output.parent.mkdir(parents=True,exist_ok=True)
    paths.configure_geospatial_environment()
    import rasterio,pyproj
    result=dict(dependencies=paths.dependency_status(),gdal_version=rasterio.__gdal_version__,rasterio_source=rasterio.__file__,
                python_version=sys.version,python_version_info=list(sys.version_info[:3]),
                proj_version=pyproj.proj_version_str,pyproj_data=pyproj.datadir.get_data_dir(),
                packages={n:bool(importlib.util.find_spec(n)) for n in ('torch','kornia','einops','yaml','PyInstaller','osgeo','fiona','qgis','arcpy')})
    if scan:result['scan']=scan_project(paths.get_app_base_path())
    launcher=paths.get_otb_path()/'bin'/'otbApplicationLauncherCommandLine.exe'
    if launcher.is_file():
        try:
            cp=subprocess.run([str(launcher),'ReadImageInfo','-help'],env=paths.get_otb_environment(),capture_output=True,
                              text=True,errors='replace',timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            result['otb_test']=dict(returncode=cp.returncode,output=(cp.stdout+cp.stderr)[-4000:])
        except Exception as exc:result['otb_test']=dict(error=str(exc))
    else:result['otb_test']='OPTIONAL MISSING'
    if smoke:
        import numpy as np,cv2
        result['opencv_version']=cv2.__version__
        from shapely.geometry import box
        assert box(0,0,2,2).intersection(box(1,1,3,3)).area==1
        import engine
        sample=np.full((32,32,3),90,np.uint8)
        difference,metrics=engine.compare(sample,sample,'SSIM')
        assert not difference.any() and metrics['similarity_percent']>99.9
        assert cv2.SIFT_create() is not None
        result['geometry_comparison_smoke']='PASS'
        from rasterio.transform import from_origin
        with tempfile.TemporaryDirectory(dir=output.parent) as folder:
            p=Path(folder)/'smoke.tif';arr=np.full((3,32,32),90,np.uint8)
            with rasterio.open(p,'w',driver='GTiff',width=32,height=32,count=3,dtype='uint8',crs='EPSG:32638',
                               transform=from_origin(500000,2800000,.5,.5),compress='deflate') as ds:ds.write(arr)
            with rasterio.open(p) as ds:assert np.array_equal(ds.read(),arr)
            if launcher.is_file():
                cp=subprocess.run([str(launcher),'ReadImageInfo','-in',str(p.resolve())],env=paths.get_otb_environment(),
                                  capture_output=True,text=True,errors='replace',timeout=30,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                result['otb_image_test']=dict(returncode=cp.returncode,output=(cp.stdout+cp.stderr)[-3000:])
                assert cp.returncode==0,result['otb_image_test']
            assert cv2.imdecode(np.fromfile(p,dtype=np.uint8),cv2.IMREAD_UNCHANGED) is not None
            lon,lat=pyproj.Transformer.from_crs(32638,4326,always_xy=True).transform(500000,2800000)
            assert abs(lon-45)<.001 and 25<lat<26
            from unittest.mock import patch
            import main
            app=main.QApplication.instance() or main.QApplication([])
            with patch.object(Path,'home',return_value=Path(folder).resolve()),patch.object(main.MainWindow,'_cleanup_stale_temp_files'):
                window=main.MainWindow();window.show();app.processEvents();result['window_icon']=not window.windowIcon().isNull()
                before=np.full((3,128,128),45,np.uint8)
                before[:,16:48,16:48]=190;before[:,76:112,74:110]=130
                after=before.copy();after[:,53:76,55:78]=235
                pair=[]
                for name,array in (('binary_before.tif',before),('binary_after.tif',after)):
                    source=Path(folder)/name
                    with rasterio.open(source,'w',driver='GTiff',width=128,height=128,count=3,dtype='uint8',
                                       crs='EPSG:32638',transform=from_origin(500000,2800000,.5,.5)) as ds:ds.write(array)
                    pair.append(source)
                window._load_a(str(pair[0]));window._load_b(str(pair[1]))
                assert window.a is not None and window.b is not None
                result['ui_image_import']='PASS'
                from target_management import DEFAULT_COLUMNS, SpreadsheetEditorDialog
                target_rows=[
                    {'id': 15, 'target_uid': 'binary-15', 'target_name': '', 'longitude': 46.71,
                     'latitude': 24.71, 'target_state': 'مهم', 'report_included': False},
                    {'id': 20, 'target_uid': 'binary-20', 'target_name': 'مبنى خاص',
                     'report_included': False},
                ]
                editor=SpreadsheetEditorDialog(target_rows,DEFAULT_COLUMNS,{},window,
                                               image_paths=(str(pair[0]),str(pair[1])))
                editor.refresh_metadata_from_images()
                assert editor.metadata_sources.get('longitude') and editor.metadata_sources.get('latitude')
                name_column=next(i for i,c in enumerate(c for c in editor.columns if c.get('visible',True))
                                 if c.get('key')=='target_name')
                editor.table.selectRow(0);editor.toggle_report_selection()
                editor.table.item(0,name_column).setText('طائرة')
                editor.accept()
                assert editor.regions[0]['target_name']=='طائرة'
                assert editor.regions[0]['report_included'] and not editor.regions[1]['report_included']
                result['ui_excel_target_selection']='PASS: packaged 13-column editor extracted CRS, selected target ID 15, and saved user target name'
                from report_export import export_docx, export_pdf, report_html
                snapshot={'report_metadata':editor.metadata,'region_columns':editor.columns,
                          'regions':editor.regions,'objects':[],'change_summary':{}}
                html=report_html(snapshot,['overview','targets'])
                assert 'طائرة' in html and '46.71' in html and 'مبنى خاص' not in html
                docx=Path(folder)/'binary_report.docx';pdf=Path(folder)/'binary_report.pdf'
                export_docx(docx,html);export_pdf(pdf,html)
                import zipfile
                with zipfile.ZipFile(docx) as archive:
                    xml=archive.read('word/document.xml').decode('utf-8')
                    assert 'طائرة' in xml and '46.71' in xml and 'مبنى خاص' not in xml
                assert pdf.read_bytes().startswith(b'%PDF-')
                result['ui_word_pdf_export']='PASS: packaged DOCX contained the selected ID/name/coordinates; PDF was generated'
                import structural_comparison as structural
                rng=np.random.default_rng(71)
                gray=cv2.GaussianBlur(rng.uniform(20,210,(384,384)).astype(np.float32),(0,0),1.2)
                valid=np.ones(gray.shape,bool);valid[:8]=False
                reference,_=structural.descriptors(gray,valid)
                moving,_=structural.descriptors(900-3*gray,valid)
                moving=cv2.warpAffine(moving,np.float32([[1,0,3],[0,1,-2]]),(384,384))
                aligned_result=[];alignment_errors=[]
                align_worker=main.AlignWorker(reference,moving,.75)
                align_worker.completed.connect(lambda *values:aligned_result.append(values))
                align_worker.failed.connect(alignment_errors.append)
                align_worker.run()
                assert aligned_result and not alignment_errors,alignment_errors
                _,_,alignment_report,_=aligned_result[0]
                assert alignment_report['alignment_approved'] and alignment_report['residual_p90_px']<1.5
                result['automatic_alignment']='PASS: packaged AlignWorker approved a shifted 384x384 pair'
                window.aligned=window.b.array.copy();window.H=np.eye(3,dtype=np.float64)
                window.valid_mask=np.full(window.a.array.shape[:2],255,np.uint8)
                window.last_alignment_report={'alignment_approved':True,'quality_ar':'identical synthetic grid'}
                window._workflow_compare_gate=lambda:None
                window.method.setCurrentText('ROBUST V6');window.run_compare()
                deadline=time.monotonic()+45
                while time.monotonic()<deadline:
                    app.processEvents()
                    if window.diff is not None and not window._worker_running(window._compare_worker):break
                    time.sleep(.02)
                assert window.diff is not None and window.compare_meta
                result['ui_comparison']='PASS'
                window.detect_changes();deadline=time.monotonic()+45
                while time.monotonic()<deadline:
                    app.processEvents()
                    if window.mask is not None or window._change_worker is None:break
                    time.sleep(.02)
                assert window.mask is not None
                result['ui_change_detection']='PASS'
                window.close();app.processEvents()
        result['smoke']='PASS'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('output');parser.add_argument('--smoke',action='store_true');parser.add_argument('--scan',action='store_true')
    args=parser.parse_args();os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    import main
    result=run(args.output,args.smoke,args.scan)
    print(json.dumps({k:v for k,v in result.items() if k!='scan'},ensure_ascii=True,indent=2))
