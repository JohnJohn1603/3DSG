cmake_minimum_required(VERSION 3.19)
project(standalone_sg_m4_dependency_probe LANGUAGES CXX)
enable_testing()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(M4_DEPENDENCY_PREFIX "${CMAKE_CURRENT_LIST_DIR}/../../build/deps/m4-prefix/usr"
    CACHE PATH "Extracted local package root (the usr/ directory) for M4 checks")
list(PREPEND CMAKE_PREFIX_PATH "${M4_DEPENDENCY_PREFIX}")
list(PREPEND CMAKE_PREFIX_PATH "${M4_DEPENDENCY_PREFIX}/lib/${CMAKE_LIBRARY_ARCHITECTURE}/cmake")

find_package(CGAL CONFIG QUIET)
find_path(GMP_INCLUDE_DIR NAMES gmp.h
  HINTS "${M4_DEPENDENCY_PREFIX}/include"
        "${M4_DEPENDENCY_PREFIX}/include/${CMAKE_LIBRARY_ARCHITECTURE}")
find_library(GMP_LIBRARY NAMES gmp HINTS "${M4_DEPENDENCY_PREFIX}/lib"
  "${M4_DEPENDENCY_PREFIX}/lib64" "${M4_DEPENDENCY_PREFIX}/lib/x86_64-linux-gnu")
find_path(MPFR_INCLUDE_DIR NAMES mpfr.h HINTS "${M4_DEPENDENCY_PREFIX}/include")
find_library(MPFR_LIBRARY NAMES mpfr HINTS "${M4_DEPENDENCY_PREFIX}/lib"
  "${M4_DEPENDENCY_PREFIX}/lib64" "${M4_DEPENDENCY_PREFIX}/lib/x86_64-linux-gnu")

message(STATUS "M4 dependency prefix: ${M4_DEPENDENCY_PREFIX}")
message(STATUS "CGAL found: ${CGAL_FOUND}; version: ${CGAL_VERSION}")
message(STATUS "GMP include: ${GMP_INCLUDE_DIR}; library: ${GMP_LIBRARY}")
message(STATUS "MPFR include: ${MPFR_INCLUDE_DIR}; library: ${MPFR_LIBRARY}")

if(CGAL_FOUND AND TARGET CGAL::CGAL AND GMP_INCLUDE_DIR AND GMP_LIBRARY
   AND MPFR_INCLUDE_DIR AND MPFR_LIBRARY)
  add_executable(m4_dependency_probe probe.cpp)
  target_include_directories(m4_dependency_probe PRIVATE
    "${GMP_INCLUDE_DIR}" "${MPFR_INCLUDE_DIR}")
  target_link_libraries(m4_dependency_probe PRIVATE
    CGAL::CGAL "${GMP_LIBRARY}" "${MPFR_LIBRARY}")
  set_target_properties(m4_dependency_probe PROPERTIES
    BUILD_RPATH "${M4_DEPENDENCY_PREFIX}/lib/${CMAKE_LIBRARY_ARCHITECTURE}")
  add_test(NAME m4_dependency_probe COMMAND m4_dependency_probe)
  message(STATUS "M4 dependency probe target enabled (CGAL + GMP + MPFR only)")
else()
  message(FATAL_ERROR "CGAL/GMP/MPFR development set incomplete; refusing configure without a real executable target")
endif()
