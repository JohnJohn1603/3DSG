# Recreating the runtime

Target: Linux x86_64. Use four separate environments: their NumPy, PyTorch and Python ABIs differ. `environments/installed-versions.json` records the actual installed distributions used for the local demo, with no source-machine paths. The three requirements files pin all installed runtime distributions; PyTorch wheel file URLs were replaced with their installed version. PyTorch3D 0.7.4 comes from the bundled source, installed as a normal package rather than an editable source path.

| Runtime | Python | Key versions |
| --- | --- | --- |
| Habitat | 3.9.23 | Habitat-Sim 0.3.3, NumPy 1.26.4 |
| ConceptGraphs | 3.10.21 | torch 2.0.1+cpu, torchvision 0.15.2+cpu, NumPy 1.24.3, Open3D 0.17.0, FAISS CPU 1.7.4, PyTorch3D 0.7.4 |
| Appearance | 3.12.3 | torch 2.6.0+cpu, torchvision 0.21.0+cpu, open_clip_torch 3.2.0 |
| Perception | 3.12.3 | NumPy 2.5.3, onnxruntime 1.24.4, Pillow 10.2.0, PyYAML 6.0.1 |

Install CPython 3.10, CPython 3.12 (including venv and development headers), and conda beforehand. A C++ compiler is required to rebuild the CPU PyTorch3D extension. Package installation downloads from PyPI, the PyTorch CPU wheel index, and the public conda channels; this is a source/research bundle, not an offline package mirror.

From the bundle root:

```bash
python3 tools/setup_environments.py --dry-run --runtime-config /tmp/3dsg-runtime.json
python3 tools/setup_environments.py --python310 python3.10 --python312 python3.12 --conda conda --jobs 2 --runtime-config /path/to/3dsg-runtime.json
```

Default environment prefixes are `.venvs/{habitat,conceptgraphs,appearance,perception}` inside this extracted bundle. Use `--venvs-dir /another/location` to put them elsewhere. The runtime JSON uses absolute interpreter paths (`habitat_python`, `conceptgraphs_python`, `appearance_python`, `perception_python`); regenerate it if the bundle or environments move. `--roles appearance perception` permits partial installation; the JSON still describes the four intended paths, so omitted roles must already exist before running pipelines that need them. The script checks Python minor versions, imports, and `pip check`.

Habitat uses `environments/habitat-linux-64.explicit.txt` to preserve exact conda package URLs/builds, followed by the actual Pillow 10.4.0 pip override. `habitat-environment.yml` is a readable version-pinned fallback export; explicit creation is more reproducible. Habitat-Sim is installed from conda, because its installed metadata appears as a pip distribution despite the actual native package being present in the conda explicit list. The simulator still needs a working OpenGL/EGL driver appropriate to the target machine and scene assets supplied by the bundle's launch workflow.

## Native non-ROS rebuild

The prebuilt workers use the adjacent `prebuilt/linux-x86_64/lib` directory through `$ORIGIN/../lib`. They require compatible Linux system libraries. To rebuild on the target host, install these development packages (Ubuntu/Debian names):

```bash
sudo apt-get install build-essential cmake pkg-config libeigen3-dev libopencv-dev libjsoncpp-dev libboost-dev
python3 tools/build_native.py --build-dir /tmp/3dsg-native-build --jobs 2
```

The build uses bundled OctoMap, CGAL/GMP/MPFR headers/libraries, MapConversion source, and the existing DuDe/FIST source/archive. It does not require ROS. MapConversion is checked against `tools/native_cmake/mapconversion-tree.json`, whose pinned commit must agree with the project source lock. This allows a `.git`-free source archive without weakening source verification to an unchecked Git bypass. The overlay builds the unchanged native source targets and makes the worker RPATH relocatable. It writes compiler intermediates outside the bundle, copies only final workers/runtime libraries into `prebuilt/linux-x86_64`, checks dynamic-library resolution with `ldd`, and refreshes that directory's `SHA256SUMS.json`.

Retain the bundled upstream notices and licensing documents, including FIST's restrictions, with source and binaries. Rebuilding does not grant any additional redistribution rights.
