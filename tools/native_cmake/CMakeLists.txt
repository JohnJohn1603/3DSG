cmake_minimum_required(VERSION 3.20)
project(3dsg_portable_native LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT BUNDLE_ROOT)
  message(FATAL_ERROR "Set BUNDLE_ROOT; invoke tools/build_native.py for source verification")
endif()
set(REBUILD_SOURCE "${BUNDLE_ROOT}/projects/Rebuild_3DScene_OccuSG")
set(STANDALONE_SG_ENABLE_M3 ON)
set(OCTOMAP_ROOT "${BUNDLE_ROOT}/third_party/octomap-prefix/usr" CACHE PATH "Vendored OctoMap prefix" FORCE)
set(MAPCONVERSION_SOURCE_DIR "${BUNDLE_ROOT}/third_party/mapconversion")
# The Python entry point verifies the full vendored tree against the pinned
# manifest. This overlay intentionally avoids the upstream .git-only check.
add_subdirectory("${REBUILD_SOURCE}/native/occupancy" occupancy)
set(M4_PREFIX "${BUNDLE_ROOT}/third_party/m4-prefix/usr" CACHE PATH "Vendored CGAL prefix" FORCE)
set(OCCUSG_SOURCE "${REBUILD_SOURCE}/ref_OccuSG/OccuSG/src/incremental_dude_ros2/incremental_dude_ros2" CACHE PATH "Vendored DuDe source" FORCE)
set(GMP_INCLUDE_DIR "${M4_PREFIX}/include/x86_64-linux-gnu" CACHE PATH "GMP headers" FORCE)
set(GMP_LIBRARIES "${M4_PREFIX}/lib/x86_64-linux-gnu/libgmp.so" CACHE FILEPATH "GMP library" FORCE)
set(MPFR_INCLUDE_DIR "${M4_PREFIX}/include" CACHE PATH "MPFR headers" FORCE)
set(MPFR_LIBRARIES "${M4_PREFIX}/lib/x86_64-linux-gnu/libmpfr.so" CACHE FILEPATH "MPFR library" FORCE)
add_subdirectory("${REBUILD_SOURCE}/native/dude" dude)
set_target_properties(occusg_dude_worker PROPERTIES
  BUILD_WITH_INSTALL_RPATH TRUE
  INSTALL_RPATH "$ORIGIN/../lib"
  BUILD_RPATH "$ORIGIN/../lib")
