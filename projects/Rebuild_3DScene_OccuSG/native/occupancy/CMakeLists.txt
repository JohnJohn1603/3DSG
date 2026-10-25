set(OCTOMAP_ROOT "" CACHE PATH "Prefix containing OctoMap include/ and lib/")

find_path(OCTOMAP_INCLUDE_DIR
  NAMES octomap/OcTree.h
  HINTS "${OCTOMAP_ROOT}/include"
  REQUIRED
)
find_library(OCTOMAP_LIBRARY
  NAMES octomap
  HINTS "${OCTOMAP_ROOT}/lib" "${OCTOMAP_ROOT}/lib/x86_64-linux-gnu"
  REQUIRED
)
find_library(OCTOMATH_LIBRARY
  NAMES octomath
  HINTS "${OCTOMAP_ROOT}/lib" "${OCTOMAP_ROOT}/lib/x86_64-linux-gnu"
  REQUIRED
)

add_library(standalone_sg_occupancy occupancy_map.cpp reference_insert_scan.cpp)
target_include_directories(standalone_sg_occupancy
  PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}" "${OCTOMAP_INCLUDE_DIR}"
)
target_link_libraries(standalone_sg_occupancy PUBLIC "${OCTOMAP_LIBRARY}" "${OCTOMATH_LIBRARY}")

if(STANDALONE_SG_ENABLE_M3)
  find_path(EIGEN3_INCLUDE_DIR NAMES Eigen/Dense HINTS /usr/include/eigen3 REQUIRED)
  target_sources(standalone_sg_occupancy PRIVATE projection_backend.cpp
    "${MAPCONVERSION_SOURCE_DIR}/mapconversion/src/MapConverter.cpp")
  target_include_directories(standalone_sg_occupancy
    PUBLIC "${MAPCONVERSION_SOURCE_DIR}/mapconversion/include" "${EIGEN3_INCLUDE_DIR}")
  target_compile_definitions(standalone_sg_occupancy PUBLIC STANDALONE_SG_WITH_M3=1)
  add_executable(standalone_sg_m3_projection_smoke projection_backend_smoke.cpp)
  target_link_libraries(standalone_sg_m3_projection_smoke PRIVATE standalone_sg_occupancy)
endif()

add_executable(standalone_sg_octomap_fixture_runner fixture_runner.cpp)
target_link_libraries(standalone_sg_octomap_fixture_runner PRIVATE standalone_sg_occupancy)
