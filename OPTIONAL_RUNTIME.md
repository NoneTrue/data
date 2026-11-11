# Optional OTB and GeoAlignAI resources

These resources are not required by ChangeVision's current analysis engine. Installing them does not enable a new analysis backend.

The optional runtime release is published at https://github.com/NoneTrue/data/releases/tag/optional-runtimes-20261111. It contains the unchanged official OTB 9.1.0 Windows archive and a separate GeoAlignAI source archive containing only redistributable LoFTR and YACS sources. `runtime-manifest.json` lists the asset sizes and SHA-256 hashes. The Windows publishing workflow checks the OTB launcher and source integrity before making the release public.

On Windows, from a source checkout, install either or both resources into `runtime/`:

```powershell
py -3.11 setup_optional_runtime.py --component otb
py -3.11 setup_optional_runtime.py --component geoalignai
```

The installer validates the release hashes, ZIP paths and destination folders. It refuses to overwrite an existing runtime directory. OTB's bundled Python bindings target Python 3.10; use its command line executables in a separate process rather than importing those bindings into ChangeVision's Python 3.11 environment.

The SuperGlue and SuperPoint repositories and model weights are distributed by Magic Leap under terms that do not allow us to republish them. They are therefore absent from this repository and release. If you have reviewed and accept their license for your own local use, the opt-in helper downloads their pinned sources directly from the owner and copies the needed weights into your local GeoAlignAI folder:

```powershell
py -3.11 fetch_restricted_geoalign.py --accept-magicleap-license
```

Read the [SuperGlue license](https://github.com/magicleap/SuperGluePretrainedNetwork/blob/master/LICENSE) and [SuperPoint license](https://github.com/magicleap/SuperPointPretrainedNetwork/blob/master/LICENSE) before running that command. This does not install PyTorch or enable model inference in ChangeVision. The local helper is separate from the public release and does not grant redistribution rights.
