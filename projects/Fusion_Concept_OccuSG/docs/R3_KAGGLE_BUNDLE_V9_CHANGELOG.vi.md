# Bundle v9 changelog

Supersedes bundle v8 for R3 preparation. Bundle v8 and its manifest are retained unchanged.

- Pins GradSLAM and chamferdist full source commits using the locked ConceptGraphs package versions and a documented downstream ConceptGraphs-derived recipe. Runtime compatibility remains unverified.
- Separates expected upstream checksums from local measured file SHA-256; records checkpoint source revisions/provenance gaps. RAM download from mutable `main` was removed from the gated download cell.
- Makes repository checkout cells fetch exact commit SHAs and verify detached HEAD; no default-branch checkout is used.
- Pins the OpenCLIP model repository revision in the lock and staging cell, while marking reference-loader enforcement unresolved.
- Adds private Kaggle/HM3D rights checklist and cell-by-cell execution guidance. Smoke and seven-frame pilot write to distinct outputs under a per-execution run tag.
- No package install, checkpoint download, upload, GPU job, capture or inference was performed while preparing v9.
