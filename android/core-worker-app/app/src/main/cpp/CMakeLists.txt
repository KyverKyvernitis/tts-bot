# Referência/auditoria dos binários nativos do Core Worker.
#
# O build privado feito pelo phone worker/Termux normalmente empacota libs
# prebuilt em app/src/main/jniLibs/arm64-v8a, porque CMake do SDK Manager pode
# não executar no Android ARM64. Este arquivo mantém a definição correta para
# builds externos/NDK e para auditoria dos fontes.

cmake_minimum_required(VERSION 3.22.1)

project(coreworker_native LANGUAGES C CXX)

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

add_library(coreworker_runner SHARED
        coreworker_runner.c)

target_compile_options(coreworker_runner PRIVATE
        -Wall
        -Wextra
        -Werror
        -fvisibility=hidden)

set_target_properties(coreworker_runner PROPERTIES
        OUTPUT_NAME "coreworker_runner"
        C_STANDARD 11
        C_STANDARD_REQUIRED YES
        C_EXTENSIONS NO)
