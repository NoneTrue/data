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

They are not automatically uploaded or downloaded. Including such folders would
not enable a new backend. Third-party model inference would require a separately
implemented and tested integration and a review of the applicable model licenses.

Runtime diagnostics intentionally report these as optional. The application
icon files in `runtime/icons` are included in this repository.
