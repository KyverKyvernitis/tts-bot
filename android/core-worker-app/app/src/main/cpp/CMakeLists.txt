# Patch 84.2 note:
# Este CMakeLists fica como referência/auditoria do executor nativo.
# O build privado feito pelo phone worker/Termux não usa externalNativeBuild,
# porque o CMake instalado pelo Android SDK Manager não executa corretamente
# dentro do Termux/Android ARM64. O APK empacota a biblioteca prebuilt em
# app/src/main/jniLibs/arm64-v8a/libcoreworker_executor.so.

cmake_minimum_required(VERSION 3.22.1)

project(coreworker_executor LANGUAGES CXX)

add_library(coreworker_executor SHARED
        coreworker_executor.cpp)

target_compile_features(coreworker_executor PRIVATE cxx_std_17)

target_compile_options(coreworker_executor PRIVATE
        -Wall
        -Wextra
        -Werror
        -fvisibility=hidden)

set_target_properties(coreworker_executor PROPERTIES
        CXX_STANDARD 17
        CXX_STANDARD_REQUIRED YES
        CXX_EXTENSIONS NO)
