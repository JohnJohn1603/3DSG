# M4 native dependency-prefix probe

This isolated probe checks discovery and a real compile/link/run path for CGAL,
GMP and MPFR only. It runs the pinned DuDe-style CGAL constrained-Delaunay API
on a tiny synthetic square, but does not configure, include, build, link, or
run DuDe or FIST.

The reserved local prefix is `build/deps/m4-prefix`. Populate it only from
individually reviewed, checksummed development packages/source archives; do not
assume this project has redistribution rights for those artifacts. The probe
also permits CMake's normal system search so an existing development install
can be tested without copying it into the prefix.

The verified Ubuntu Noble amd64 artifacts and SHA-256 values are recorded in
`locks/sources.json`. To reproduce extraction without system installation, from
the project root:

```sh
mkdir -p build/deps/m4-cache build/deps/m4-prefix
cd build/deps/m4-cache
apt-get download libcgal-dev=5.6-1build3 libgmp-dev=2:6.3.0+dfsg-2ubuntu6.1 \
  libmpfr-dev=4.2.1-1build1.1 libgmp10=2:6.3.0+dfsg-2ubuntu6.1 \
  libmpfr6=4.2.1-1build1.1
sha256sum *.deb
for p in *.deb; do dpkg-deb -x "$p" ../m4-prefix; done
cd ../../..
```

Then, from the project root:

```sh
cmake -S native/m4_dependency_probe -B build/native-m4-dependency-probe \
  -DM4_DEPENDENCY_PREFIX="$PWD/build/deps/m4-prefix/usr" \
  -DCMAKE_PREFIX_PATH="$PWD/build/deps/m4-prefix/usr"
cmake --build build/native-m4-dependency-probe --parallel 2
ctest --test-dir build/native-m4-dependency-probe --output-on-failure
```

If any required CGAL/GMP/MPFR component is absent, configure fails rather than
reporting a configure-only pass. A passing probe is not a DuDe build, a license
clearance, or M4 acceptance. The probe uses the GPL-licensed CGAL 5.6 2D
Triangulations/TDS packages; do not redistribute its executable without an
independent license review.
