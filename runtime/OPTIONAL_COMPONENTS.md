# Optional development components

The current analysis application does not call OTB, GeoAlignAI or model inference.
Its Python dependencies are in the repository requirements files.

The development workspace contains these optional resources, not needed for
the source tests or normal analysis/report workflow:

| Directory | Approximate development size | Role |
| --- | ---: | --- |
| OTB-9.1.0-Win64 | 645 MB | Optional OTB diagnostics plus libraries, headers and development data |
| GeoAlignAI | 360 MB | Third-party repositories and duplicate model weights, without a connected UI backend |
| models | 5.35 MB | Development weights and dataset/class configuration; not loaded by the analysis engine |

The official OTB Windows archive and a redistributable LoFTR/YACS source subset
are published separately from the source repository. The restricted SuperGlue and
SuperPoint sources and weights are not republished. None of these resources is
downloaded by normal application setup or enables a new backend. See
`OPTIONAL_RUNTIME.md` for the separate release and explicit installation steps.

Runtime diagnostics intentionally report these as optional. The application
icon files in `runtime/icons` are included in this repository.
