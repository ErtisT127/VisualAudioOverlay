cmake_minimum_required(VERSION 3.20)
project(overlay_native LANGUAGES CXX)

add_library(overlay_native SHARED src/overlay_native.cpp)
target_compile_features(overlay_native PRIVATE cxx_std_17)
target_compile_definitions(overlay_native PRIVATE OVERLAY_NATIVE_BUILD UNICODE _UNICODE NOMINMAX)
target_include_directories(overlay_native PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

if(MINGW)
    target_compile_options(overlay_native PRIVATE -municode)
    target_link_options(overlay_native PRIVATE -static -static-libgcc -static-libstdc++)
elseif(MSVC)
    # Keep the DLL self-contained on machines without the VC++ redistributable.
    set_property(TARGET overlay_native PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

target_link_libraries(overlay_native PRIVATE
    d3d11 dxgi dcomp d2d1 dwmapi user32 gdi32 ole32 uuid
)

set_target_properties(overlay_native PROPERTIES
    OUTPUT_NAME "overlay_native"
    PREFIX ""
    CXX_EXTENSIONS OFF
)
