cmake_minimum_required(VERSION 3.20)
project(occusg_dude_worker LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE OFF)

get_filename_component(WORKSPACE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(OCCUSG_SOURCE "${WORKSPACE_ROOT}/ref_OccuSG/OccuSG/src/incremental_dude_ros2/incremental_dude_ros2"
    CACHE PATH "Pinned OccuSG package containing Incremental DuDe")
set(M4_PREFIX "${WORKSPACE_ROOT}/build/deps/m4-prefix/usr" CACHE PATH "Local CGAL/GMP/MPFR prefix")

if(NOT EXISTS "${OCCUSG_SOURCE}/include/inc_decomp.cpp")
  message(FATAL_ERROR "Pinned Incremental DuDe source not found: ${OCCUSG_SOURCE}")
endif()

find_package(OpenCV REQUIRED COMPONENTS core imgproc)
find_package(CGAL REQUIRED COMPONENTS Core PATHS "${M4_PREFIX}/lib/x86_64-linux-gnu/cmake/CGAL" NO_DEFAULT_PATH)
find_package(PkgConfig REQUIRED)
pkg_check_modules(JSONCPP REQUIRED IMPORTED_TARGET jsoncpp)

set(GENERATED_DIR "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${GENERATED_DIR}")
foreach(source IN ITEMS "${OCCUSG_SOURCE}/include/wrapper.cpp" "${OCCUSG_SOURCE}/include/inc_decomp.cpp")
  get_filename_component(name "${source}" NAME)
  file(READ "${source}" contents)
  string(REPLACE "#include \"dude_ros_logging.hpp\"" "#include \"m4_non_ros_logging.hpp\"" contents "${contents}")
  file(WRITE "${GENERATED_DIR}/${name}" "${contents}")
endforeach()

file(GLOB DUDE_ROOT_SOURCES CONFIGURE_DEPENDS "${OCCUSG_SOURCE}/Third_Party/dude_final/src/*.cpp")
file(GLOB DUDE_SKELETON_SOURCES CONFIGURE_DEPENDS "${OCCUSG_SOURCE}/Third_Party/dude_final/src/skeleton/*.cpp")
file(GLOB DUDE_GRAPH_SOURCES CONFIGURE_DEPENDS "${OCCUSG_SOURCE}/Third_Party/dude_final/src/graph/*.cpp")
file(GLOB DUDE_DRAW_SOURCES CONFIGURE_DEPENDS "${OCCUSG_SOURCE}/Third_Party/dude_final/src/draw/*.cpp")
# The upstream ROS target compiles these same GLOB groups. The standalone target
# excludes only the ROS nodes/evaluation and links the existing FIST archive.
list(FILTER DUDE_ROOT_SOURCES EXCLUDE REGEX "/main\.cpp$")
set(DUDE_SOURCES ${DUDE_ROOT_SOURCES} ${DUDE_SKELETON_SOURCES} ${DUDE_GRAPH_SOURCES} ${DUDE_DRAW_SOURCES})

add_executable(occusg_dude_worker
  worker.cpp globals.cpp preprocessing.cpp
  "${GENERATED_DIR}/wrapper.cpp" "${GENERATED_DIR}/inc_decomp.cpp"
  ${DUDE_SOURCES})

set(DUDE_ROOT "${OCCUSG_SOURCE}/Third_Party/dude_final")
target_include_directories(occusg_dude_worker BEFORE PRIVATE
  "${CMAKE_CURRENT_SOURCE_DIR}/include"
  "${OCCUSG_SOURCE}/include"
  "${DUDE_ROOT}/src"
  "${DUDE_ROOT}/src/skeleton"
  "${DUDE_ROOT}/src/graph"
  "${DUDE_ROOT}/src/draw"
  "${DUDE_ROOT}/mathtool"
  "${DUDE_ROOT}/FIST"
  "${DUDE_ROOT}"
  ${OpenCV_INCLUDE_DIRS})
target_link_directories(occusg_dude_worker PRIVATE "${M4_PREFIX}/lib/x86_64-linux-gnu")
target_link_libraries(occusg_dude_worker PRIVATE
  ${OpenCV_LIBS} CGAL::CGAL CGAL::CGAL_Core
  "${DUDE_ROOT}/FIST/libFIST.linux64.a"
  gmpxx gmp mpfr PkgConfig::JSONCPP)
target_compile_definitions(occusg_dude_worker PRIVATE NDEBUG)
target_compile_options(occusg_dude_worker PRIVATE -Wall -Wextra -Wno-deprecated-declarations)
target_link_options(occusg_dude_worker PRIVATE -no-pie)
set_target_properties(occusg_dude_worker PROPERTIES
  BUILD_RPATH "${M4_PREFIX}/lib/x86_64-linux-gnu")
