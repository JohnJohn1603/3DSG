#----------------------------------------------------------------
# Generated CMake target import file for configuration "None".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "octomath" for configuration "None"
set_property(TARGET octomath APPEND PROPERTY IMPORTED_CONFIGURATIONS NONE)
set_target_properties(octomath PROPERTIES
  IMPORTED_LOCATION_NONE "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomath.so.1.9.7"
  IMPORTED_SONAME_NONE "liboctomath.so.1.9"
  )

list(APPEND _cmake_import_check_targets octomath )
list(APPEND _cmake_import_check_files_for_octomath "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomath.so.1.9.7" )

# Import target "octomath-static" for configuration "None"
set_property(TARGET octomath-static APPEND PROPERTY IMPORTED_CONFIGURATIONS NONE)
set_target_properties(octomath-static PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_NONE "CXX"
  IMPORTED_LOCATION_NONE "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomath.a"
  )

list(APPEND _cmake_import_check_targets octomath-static )
list(APPEND _cmake_import_check_files_for_octomath-static "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomath.a" )

# Import target "octomap" for configuration "None"
set_property(TARGET octomap APPEND PROPERTY IMPORTED_CONFIGURATIONS NONE)
set_target_properties(octomap PROPERTIES
  IMPORTED_LOCATION_NONE "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomap.so.1.9.7"
  IMPORTED_SONAME_NONE "liboctomap.so.1.9"
  )

list(APPEND _cmake_import_check_targets octomap )
list(APPEND _cmake_import_check_files_for_octomap "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomap.so.1.9.7" )

# Import target "octomap-static" for configuration "None"
set_property(TARGET octomap-static APPEND PROPERTY IMPORTED_CONFIGURATIONS NONE)
set_target_properties(octomap-static PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_NONE "CXX"
  IMPORTED_LOCATION_NONE "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomap.a"
  )

list(APPEND _cmake_import_check_targets octomap-static )
list(APPEND _cmake_import_check_files_for_octomap-static "${_IMPORT_PREFIX}/lib/x86_64-linux-gnu/liboctomap.a" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
