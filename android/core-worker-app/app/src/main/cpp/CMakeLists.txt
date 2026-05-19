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
