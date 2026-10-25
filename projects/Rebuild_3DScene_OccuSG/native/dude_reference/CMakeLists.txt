cmake_minimum_required(VERSION 3.20)
project(occusg_dude_source_reference LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
get_filename_component(WORKSPACE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
set(OCCUSG_SOURCE "${WORKSPACE_ROOT}/ref_OccuSG/OccuSG/src/incremental_dude_ros2/incremental_dude_ros2" CACHE PATH "Pinned source")
set(M4_PREFIX "${WORKSPACE_ROOT}/build/deps/m4-prefix/usr" CACHE PATH "Local native prefix")
find_package(OpenCV REQUIRED COMPONENTS core imgproc)
find_package(CGAL REQUIRED COMPONENTS Core PATHS "${M4_PREFIX}/lib/x86_64-linux-gnu/cmake/CGAL" NO_DEFAULT_PATH)
find_package(PkgConfig REQUIRED)
pkg_check_modules(JSONCPP REQUIRED IMPORTED_TARGET jsoncpp)
set(DUDE_ROOT "${OCCUSG_SOURCE}/Third_Party/dude_final")
set(GEN "${CMAKE_CURRENT_BINARY_DIR}/generated")
file(MAKE_DIRECTORY "${GEN}")
foreach(src IN ITEMS "${OCCUSG_SOURCE}/include/wrapper.cpp" "${OCCUSG_SOURCE}/include/inc_decomp.cpp")
  get_filename_component(name "${src}" NAME)
  file(READ "${src}" data)
  string(REPLACE "#include \"dude_ros_logging.hpp\"" "#include \"m4_non_ros_logging.hpp\"" data "${data}")
  file(WRITE "${GEN}/${name}" "${data}")
endforeach()
file(GLOB root_src "${DUDE_ROOT}/src/*.cpp")
file(GLOB skel_src "${DUDE_ROOT}/src/skeleton/*.cpp")
file(GLOB graph_src "${DUDE_ROOT}/src/graph/*.cpp")
file(GLOB draw_src "${DUDE_ROOT}/src/draw/*.cpp")
list(FILTER root_src EXCLUDE REGEX "/main\.cpp$")
add_executable(occusg_dude_source_reference
  "${WORKSPACE_ROOT}/native/dude/source_reference.cpp"
  "${WORKSPACE_ROOT}/native/dude/globals.cpp"
  "${WORKSPACE_ROOT}/native/dude/preprocessing.cpp"
  "${GEN}/wrapper.cpp" "${GEN}/inc_decomp.cpp"
  ${root_src} ${skel_src} ${graph_src} ${draw_src})
target_include_directories(occusg_dude_source_reference BEFORE PRIVATE
  "${WORKSPACE_ROOT}/native/dude/include" "${OCCUSG_SOURCE}/include"
  "${DUDE_ROOT}/src" "${DUDE_ROOT}/src/skeleton" "${DUDE_ROOT}/src/graph"
  "${DUDE_ROOT}/src/draw" "${DUDE_ROOT}/mathtool" "${DUDE_ROOT}/FIST"
  "${DUDE_ROOT}" ${OpenCV_INCLUDE_DIRS})
target_link_directories(occusg_dude_source_reference PRIVATE "${M4_PREFIX}/lib/x86_64-linux-gnu")
target_link_libraries(occusg_dude_source_reference PRIVATE ${OpenCV_LIBS} CGAL::CGAL CGAL::CGAL_Core
  "${DUDE_ROOT}/FIST/libFIST.linux64.a" gmpxx gmp mpfr PkgConfig::JSONCPP)
target_compile_definitions(occusg_dude_source_reference PRIVATE NDEBUG)
target_link_options(occusg_dude_source_reference PRIVATE -no-pie)
set_target_properties(occusg_dude_source_reference PROPERTIES BUILD_RPATH "${M4_PREFIX}/lib/x86_64-linux-gnu")
