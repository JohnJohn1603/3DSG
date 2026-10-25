cmake_minimum_required(VERSION 3.19)
project(scope_bridge_instrumented_m2_m3 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(OCTOMAP_ROOT "" CACHE PATH "Existing OctoMap prefix; no package install")
if(NOT OCTOMAP_ROOT)
  message(FATAL_ERROR "Pass -DOCTOMAP_ROOT to the already-existing dependency prefix")
endif()
find_path(OCTOMAP_INCLUDE_DIR NAMES octomap/OcTree.h HINTS "${OCTOMAP_ROOT}/include" REQUIRED)
find_file(OCTOMAP_STATIC NAMES liboctomap.a HINTS "${OCTOMAP_ROOT}/lib/x86_64-linux-gnu" REQUIRED)
find_file(OCTOMATH_STATIC NAMES liboctomath.a HINTS "${OCTOMAP_ROOT}/lib/x86_64-linux-gnu" REQUIRED)
find_path(EIGEN3_INCLUDE_DIR NAMES Eigen/Dense HINTS /usr/include/eigen3 REQUIRED)

add_executable(scope_bridge_m2_m3_worker
  ../occupancy/fixture_runner.cpp
  ../occupancy/occupancy_map.cpp
  ../occupancy/reference_insert_scan.cpp
  ../occupancy/projection_backend.cpp
  mapconversion/src/MapConverter.cpp
)
target_include_directories(scope_bridge_m2_m3_worker BEFORE PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/mapconversion/include"
  "${CMAKE_CURRENT_SOURCE_DIR}/../occupancy"
  "${OCTOMAP_INCLUDE_DIR}"
  "${EIGEN3_INCLUDE_DIR}"
)
target_compile_definitions(scope_bridge_m2_m3_worker PRIVATE STANDALONE_SG_WITH_M3=1)
target_link_libraries(scope_bridge_m2_m3_worker PRIVATE "${OCTOMAP_STATIC}" "${OCTOMATH_STATIC}")
target_compile_options(scope_bridge_m2_m3_worker PRIVATE -Wall -Wextra -Wpedantic)
