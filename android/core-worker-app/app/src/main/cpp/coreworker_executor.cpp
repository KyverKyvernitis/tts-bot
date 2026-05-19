#include <jni.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <unistd.h>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace {

constexpr const char* kExecutorVersion = "coreworker-executor/0.1.1-patch84.2-source";
constexpr std::size_t kArgLimit = 4096;

std::string from_jstring(JNIEnv* env, jstring value) {
    if (value == nullptr) return "";
    const char* chars = env->GetStringUTFChars(value, nullptr);
    if (chars == nullptr) return "";
    std::string out(chars);
    env->ReleaseStringUTFChars(value, chars);
    return out;
}

std::string truncate(std::string value, std::size_t limit) {
    if (value.size() <= limit) return value;
    return value.substr(0, limit) + "...";
}

std::string json_escape(const std::string& value) {
    std::ostringstream out;
    for (unsigned char ch : value) {
        switch (ch) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (ch < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", ch);
                    out << buf;
                } else {
                    out << ch;
                }
        }
    }
    return out.str();
}

bool is_allowed_command(const std::string& command) {
    static const std::vector<std::string> allowed = {
            "version", "native-ping", "echo", "env-info", "fs-probe"
    };
    for (const auto& item : allowed) {
        if (command == item) return true;
    }
    return false;
}

std::string abi_name() {
#if defined(__aarch64__)
    return "arm64-v8a";
#elif defined(__arm__)
    return "armeabi-v7a";
#elif defined(__x86_64__)
    return "x86_64";
#elif defined(__i386__)
    return "x86";
#else
    return "unknown";
#endif
}

std::string android_api() {
#if defined(__ANDROID_API__)
    return std::to_string(__ANDROID_API__);
#else
    return "unknown";
#endif
}

bool safe_workdir(const std::string& path) {
    if (path.empty()) return false;
    if (path.find('\0') != std::string::npos) return false;
    if (path.find("/../") != std::string::npos) return false;
    if (path == "/" || path == "/data" || path == "/sdcard" || path == "/storage") return false;
    return path.find("/files/core-linux") != std::string::npos;
}

bool ensure_dir(const std::string& path) {
    struct stat st{};
    if (stat(path.c_str(), &st) == 0) return S_ISDIR(st.st_mode);
    return mkdir(path.c_str(), 0700) == 0 || errno == EEXIST;
}

std::string result_json(const std::string& command,
                        bool ok,
                        int exit_code,
                        const std::string& stdout_text,
                        const std::string& stderr_text,
                        const std::string& details_json = "{}") {
    std::ostringstream out;
    out << "{"
        << "\"ok\":" << (ok ? "true" : "false") << ","
        << "\"exitCode\":" << exit_code << ","
        << "\"command\":\"" << json_escape(command) << "\","
        << "\"stdout\":\"" << json_escape(truncate(stdout_text, 8192)) << "\","
        << "\"stderr\":\"" << json_escape(truncate(stderr_text, 4096)) << "\","
        << "\"details\":" << (details_json.empty() ? "{}" : details_json)
        << "}";
    return out.str();
}

std::string env_info_json() {
    struct utsname uts{};
    const bool has_uts = uname(&uts) == 0;
    std::ostringstream details;
    details << "{"
            << "\"executorVersion\":\"" << json_escape(kExecutorVersion) << "\","
            << "\"abi\":\"" << json_escape(abi_name()) << "\","
            << "\"androidApi\":\"" << json_escape(android_api()) << "\","
            << "\"pid\":" << static_cast<long long>(getpid()) << ","
            << "\"uid\":" << static_cast<long long>(getuid()) << ","
            << "\"utsSysname\":\"" << json_escape(has_uts ? uts.sysname : "") << "\","
            << "\"utsMachine\":\"" << json_escape(has_uts ? uts.machine : "") << "\""
            << "}";
    std::ostringstream stdout_text;
    stdout_text << "executor=" << kExecutorVersion
                << " abi=" << abi_name()
                << " api=" << android_api();
    return result_json("env-info", true, 0, stdout_text.str(), "", details.str());
}

std::string fs_probe_json(const std::string& workdir) {
    if (!safe_workdir(workdir)) {
        return result_json("fs-probe", false, 64, "", "workDir recusado pelo executor nativo", "{\"workDirAccepted\":false}");
    }
    struct stat st{};
    const bool exists = stat(workdir.c_str(), &st) == 0;
    const bool is_dir = exists && S_ISDIR(st.st_mode);
    const bool readable = is_dir && access(workdir.c_str(), R_OK) == 0;
    const bool writable = is_dir && access(workdir.c_str(), W_OK) == 0;
    std::string runtime_dir = workdir + "/runtime";
    bool runtime_ok = writable && ensure_dir(runtime_dir);
    std::string marker = runtime_dir + "/native-fs-probe.txt";
    bool marker_ok = false;
    if (runtime_ok) {
        std::ofstream fh(marker, std::ios::out | std::ios::trunc);
        if (fh.good()) {
            fh << kExecutorVersion << "\n";
            marker_ok = true;
        }
    }
    const bool ok = exists && is_dir && readable && writable && runtime_ok && marker_ok;
    std::ostringstream details;
    details << "{"
            << "\"workDirAccepted\":true,"
            << "\"exists\":" << (exists ? "true" : "false") << ","
            << "\"isDir\":" << (is_dir ? "true" : "false") << ","
            << "\"readable\":" << (readable ? "true" : "false") << ","
            << "\"writable\":" << (writable ? "true" : "false") << ","
            << "\"runtimeDirOk\":" << (runtime_ok ? "true" : "false") << ","
            << "\"markerOk\":" << (marker_ok ? "true" : "false") << ","
            << "\"marker\":\"" << json_escape(marker) << "\""
            << "}";
    std::ostringstream stdout_text;
    stdout_text << "fs-probe exists=" << (exists ? "true" : "false")
                << " readable=" << (readable ? "true" : "false")
                << " writable=" << (writable ? "true" : "false")
                << " marker=" << (marker_ok ? "ok" : "failed");
    return result_json("fs-probe", ok, ok ? 0 : 65, stdout_text.str(), ok ? "" : std::strerror(errno), details.str());
}

std::string dispatch(const std::string& raw_command, const std::string& raw_arg, const std::string& workdir) {
    const std::string command = truncate(raw_command, 80);
    const std::string arg = truncate(raw_arg, kArgLimit);
    if (!is_allowed_command(command)) {
        return result_json(command, false, 126, "", "comando não permitido pelo executor nativo");
    }
    if (command == "version") {
        return result_json(command, true, 0, kExecutorVersion, "", "{\"allowlistOnly\":true}");
    }
    if (command == "native-ping") {
        return result_json(command, true, 0, "pong", "", "{\"pong\":true}");
    }
    if (command == "echo") {
        return result_json(command, true, 0, arg, "", "{\"truncated\":" + std::string(raw_arg.size() > kArgLimit ? "true" : "false") + "}");
    }
    if (command == "env-info") {
        return env_info_json();
    }
    if (command == "fs-probe") {
        return fs_probe_json(workdir);
    }
    return result_json(command, false, 127, "", "comando não implementado");
}

}  // namespace

extern "C" JNIEXPORT jstring JNICALL
Java_dev_core_worker_CoreWorkerNativeExecutor_nativeRun(JNIEnv* env, jclass, jstring command, jstring argument, jstring workdir) {
    std::string json = dispatch(from_jstring(env, command), from_jstring(env, argument), from_jstring(env, workdir));
    return env->NewStringUTF(json.c_str());
}
