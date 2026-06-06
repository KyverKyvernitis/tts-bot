package dev.core.worker;

import android.content.Context;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.TimeUnit;

/**
 * Runtime mínimo do Core Linux sem Termux.
 *
 * Esta classe mantém a etapa v1 segura: cria/valida apenas um rootfs scaffold
 * controlado no armazenamento privado do app e compõe o smoke test com o executor
 * JNI allowlist. Não baixa binários, não abre shell, não inicia Bedrock e não
 * substitui o runner real por comandos arbitrários vindos da VPS.
 */
public final class CoreLinuxRuntimeManager {
    private static final String ROOTFS_MANIFEST_SCHEMA = "core-worker-rootfs-manifest-v1";
    private static final String ROOTFS_KIND = "core-worker-rootfs-scaffold";
    private static final String ROOTFS_REAL_KIND = "core-worker-rootfs-real";
    private static final long MIN_RECOMMENDED_FREE_BYTES = 512L * 1024L * 1024L;
    private static final int TEXT_LIMIT = 12 * 1024;

    private CoreLinuxRuntimeManager() {}

    public static JSONObject rootfsSnapshot(Context context, File coreLinuxDir, String action) {
        try {
            String safeAction = clean(action, 80);
            if (safeAction.isEmpty()) safeAction = "status";
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            if ("clean_staging".equals(safeAction) || "cleanup_staging".equals(safeAction)) {
                removeTree(layout.staging);
                JSONObject state = status(layout, safeAction);
                state.put("summary", "Staging da rootfs limpo; rootfs ativa preservada");
                writeState(layout, state);
                appendLog(layout.rootfsLog, state.optString("summary"));
                return response(layout, state, safeAction);
            }
            if ("prepare".equals(safeAction) || "install".equals(safeAction) || "bootstrap".equals(safeAction)) {
                return response(layout, prepare(layout, false), safeAction);
            }
            if ("repair".equals(safeAction)) {
                return response(layout, prepare(layout, true), safeAction);
            }
            JSONObject state = status(layout, safeAction);
            if ("manifest".equals(safeAction)) {
                JSONObject existingManifest = state.optJSONObject("manifest");
                if (existingManifest == null || existingManifest.length() == 0) {
                    state.put("manifest", manifest(layout.rootfs, now(), "planned"));
                }
            }
            writeState(layout, state);
            appendLog(layout.validateLog, state.optString("summary"));
            return response(layout, state, safeAction);
        } catch (Throwable exc) {
            return error("core_linux_rootfs", exc);
        }
    }

    public static JSONObject runtimeSnapshot(Context context, File coreLinuxDir, String action, JSONObject nativeExecutor) {
        try {
            String safeAction = clean(action, 80);
            if (safeAction.isEmpty()) safeAction = "status";
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);
            JSONObject rootfsState = status(layout, safeAction);
            JSONObject executor = nativeExecutor == null ? readJson(new File(layout.runtime, "native-executor-state.json")) : nativeExecutor;
            if (executor == null) executor = new JSONObject();
            boolean executorReady = executor.optBoolean("readyForRootfs", false);
            boolean rootfsReady = rootfsState.optBoolean("rootfsReady", false);
            boolean dirsReady = layout.core.isDirectory() && layout.runtime.isDirectory() && layout.logs.isDirectory() && layout.rootfs.exists();
            JSONArray blockers = new JSONArray();
            if (!executorReady) blockers.put("executor nativo ainda não testado");
            if (!rootfsReady) blockers.put("rootfs scaffold ainda não validado");
            if (!dirsReady) blockers.put("layout core-linux incompleto");
            JSONObject state = new JSONObject();
            state.put("ok", blockers.length() == 0);
            state.put("action", safeAction);
            state.put("state", blockers.length() == 0 ? "runtime_v1_ready" : "runtime_v1_pending");
            state.put("stage", "core-linux-runtime-v1");
            state.put("coreLinuxDir", path(layout.core));
            state.put("termuxRequired", false);
            state.put("bedrockStartAllowed", false);
            state.put("pythonRequired", false);
            state.put("rootfsReady", rootfsReady);
            state.put("executorReady", executorReady);
            state.put("dirsReady", dirsReady);
            state.put("androidSdk", Build.VERSION.SDK_INT);
            state.put("rootfs", rootfsState);
            state.put("nativeExecutor", executor);
            state.put("blockers", blockers);
            state.put("warnings", new JSONArray().put("v1 é smoke test seguro; Box64/Bedrock real continuam bloqueados"));
            state.put("updatedAt", now());
            state.put("summary", blockers.length() == 0
                    ? "Core Linux Runtime v1 pronto para próximos testes sem Termux"
                    : "Core Linux Runtime v1 pendente · " + blockers.optString(0));
            writeJson(new File(layout.runtime, "linux-runtime-state.json"), state);
            return state;
        } catch (Throwable exc) {
            return error("core_linux_runtime", exc);
        }
    }

    public static JSONObject smokeTest(Context context, File coreLinuxDir, JSONObject nativeExecutor) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);

            // V12: primeiro smoke real controlado das ferramentas base. Continua sem
            // shell livre, sem comando remoto arbitrário, sem Box64 e sem Bedrock. A
            // única execução permitida aqui é uma allowlist fixa de PRoot/BusyBox
            // embutidos no APK e já aprovados pelo preflight v11.
            JSONObject executor = nativeExecutor;
            if (executor == null || !executor.optBoolean("readyForRootfs", false)) {
                executor = CoreWorkerNativeExecutor.snapshot(context, layout.core, "test");
            }
            JSONObject rootfs = rootfsSnapshot(context, layout.core, "status");
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState == null) rootfsState = rootfs;
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(context, layout.core, "smoke_v12");
            JSONObject runtime = runtimeSnapshot(context, layout.core, "smoke_v12", executor);

            boolean rootfsReal = runner.optBoolean("rootfsRealValidated", false)
                    || "real".equals(rootfsState.optString("validationLevel", ""))
                    || "rootfs_real_validated".equals(rootfsState.optString("state", ""));
            boolean baseReady = runner.optBoolean("runnerBaseRequirementsReady", false)
                    || runner.optBoolean("termuxReductionReady", false);
            JSONArray missing = runner.optJSONArray("currentMissing");
            if (missing == null) missing = runner.optJSONArray("missing");
            if (missing == null) missing = new JSONArray();

            JSONArray checks = new JSONArray()
                    .put(check("core-linux dir", layout.core.isDirectory(), path(layout.core)))
                    .put(check("executor JNI allowlist", executor.optBoolean("readyForRootfs", false), executor.optString("summary", "")))
                    .put(check("rootfs real", rootfsReal, rootfsState.optString("summary", "importe/valide rootfs real no APK")))
                    .put(check("runner/proot/busybox base", baseReady, runner.optString("summary", "base Core Linux ainda pendente")))
                    .put(check("runtime state", runtime.optBoolean("ok", false), runtime.optString("summary", "")))
                    .put(check("Bedrock bloqueado", true, "nenhum start real é feito nesta etapa"))
                    .put(check("Box64 bloqueado", true, "Box64 fica para etapa posterior ao smoke rootfs"))
                    .put(check("shell livre bloqueado", true, "somente allowlist fixa; sem comando remoto arbitrário"));

            boolean prerequisitesOk = true;
            for (int i = 0; i < checks.length(); i++) {
                JSONObject row = checks.optJSONObject(i);
                if (row != null && !row.optBoolean("ok", false)) prerequisitesOk = false;
            }

            File nativeDir = new File(context.getApplicationInfo().nativeLibraryDir == null ? "" : context.getApplicationInfo().nativeLibraryDir);
            File busyboxReal = new File(nativeDir, "libcoreworker_busybox.so");
            // V12.3: BusyBox é multi-call e decide o applet pelo basename de argv[0].
            // Executar diretamente libcoreworker_busybox.so faz o wrapper procurar um
            // applet chamado "libcoreworker_busybox.so" e retornar 127. Criamos um
            // symlink privado chamado "busybox" apontando para a native lib extraída,
            // mantendo a execução real no nativeLibraryDir e sem baixar/copiar binário.
            JSONObject busyboxLaunch = prepareBusyboxArgv0Launcher(layout.runtime, busyboxReal);
            File busybox = new File(busyboxLaunch.optString("path", path(busyboxReal)));
            File proot = new File(nativeDir, "libcoreworker_proot.so");
            JSONObject smoke = new JSONObject();
            JSONArray smokeChecks = new JSONArray();
            boolean busyboxOk = false;
            boolean prootOk = false;

            if (prerequisitesOk) {
                JSONObject busyboxSmoke = new JSONObject();
                busyboxSmoke.put("launcher", busyboxLaunch);
                JSONObject busyboxHelp = runNativeTool("busybox --help", busybox, nativeDir, layout.runtime, 8000L, "--help");
                JSONObject busyboxTrue = runNativeTool("busybox true", busybox, nativeDir, layout.runtime, 5000L, "true");
                JSONObject busyboxEcho = runNativeTool("busybox echo ok", busybox, nativeDir, layout.runtime, 5000L, "echo", "ok");
                busyboxSmoke.put("help", busyboxHelp);
                busyboxSmoke.put("true", busyboxTrue);
                busyboxSmoke.put("echo", busyboxEcho);
                busyboxOk = busyboxHelp.optBoolean("ok", false)
                        && busyboxTrue.optBoolean("ok", false)
                        && busyboxEcho.optBoolean("ok", false)
                        && "ok".equals(busyboxEcho.optString("stdout", "").trim());

                JSONObject prootSmoke = new JSONObject();
                JSONObject prootHelp = runNativeTool("proot --help", proot, nativeDir, layout.runtime, 8000L, "--help");
                JSONObject prootVersion = runNativeTool("proot --version", proot, nativeDir, layout.runtime, 8000L, "--version");
                prootSmoke.put("help", prootHelp);
                prootSmoke.put("version", prootVersion);
                prootOk = prootHelp.optBoolean("ok", false) && prootVersion.optBoolean("ok", false);

                smoke.put("busybox", busyboxSmoke);
                smoke.put("proot", prootSmoke);
                smoke.put("nativeLibraryDir", path(nativeDir));
                smoke.put("workDir", path(layout.runtime));
                smoke.put("busyboxArgv0Mode", busyboxLaunch.optString("mode", "symlink"));
                smoke.put("busyboxExecutablePath", path(busybox));
                smoke.put("env", new JSONObject()
                        .put("LD_LIBRARY_PATH", path(nativeDir))
                        .put("PROOT_LOADER", path(new File(nativeDir, "libcoreworker_proot_loader.so")))
                        .put("PROOT_LOADER_32", path(new File(nativeDir, "libcoreworker_proot_loader32.so"))));
                smokeChecks.put(check("busybox allowlist", busyboxOk, busyboxOk ? "busybox --help/true/echo ok" : "falha em uma etapa BusyBox; ver stdout/stderr"));
                smokeChecks.put(check("proot allowlist", prootOk, prootOk ? "proot --help/--version" : "falha em uma etapa PRoot; ver stdout/stderr/linker"));
            } else {
                smoke.put("skipped", true);
                smoke.put("reason", "preflight/base Core Linux ainda não está pronto");
                smokeChecks.put(check("busybox allowlist", false, "não executado: preflight/base pendente"));
                smokeChecks.put(check("proot allowlist", false, "não executado: preflight/base pendente"));
            }

            boolean ok = prerequisitesOk && busyboxOk && prootOk;
            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("type", "core_linux_runtime_smoke_test");
            out.put("state", ok ? "base_tools_smoke_ok" : (prerequisitesOk ? "base_tools_smoke_failed" : "smoke_gate_blocked"));
            out.put("stage", "core-linux-base-tools-smoke-v12");
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", false);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("runnerExecuted", false);
            out.put("runnerExecutionAllowed", false);
            out.put("prootExecuted", prerequisitesOk);
            out.put("busyboxExecuted", prerequisitesOk);
            out.put("commandsAllowlisted", true);
            out.put("commands", new JSONArray()
                    .put("busybox --help")
                    .put("busybox true")
                    .put("busybox echo ok")
                    .put("proot --help")
                    .put("proot --version"));
            out.put("checks", checks);
            out.put("smokeChecks", smokeChecks);
            out.put("missing", missing);
            out.put("nativeExecutor", executor);
            out.put("rootfs", rootfsState);
            out.put("runtime", runtime);
            out.put("runnerPreflight", runner);
            out.put("baseToolsSmoke", smoke);
            out.put("nextStep", ok ? "rootfs-proot-smoke" : "corrigir erro BusyBox/PRoot antes do rootfs smoke");
            out.put("updatedAt", now());
            out.put("summary", ok
                    ? "Smoke real Core Linux V12.3 ok · BusyBox/PRoot executaram allowlist sem Termux"
                    : (prerequisitesOk
                        ? "Smoke real Core Linux V12.3 falhou · ver stdout/stderr por etapa"
                        : "Smoke real Core Linux V12.3 bloqueado · falta base real para substituir Termux"));
            writeJson(new File(layout.runtime, "core-linux-smoke-test.json"), out);
            appendLog(new File(layout.logs, "core-linux-smoke-test.log"), out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_smoke_test", exc);
        }
    }


    public static JSONObject rootfsProotSmokeTest(Context context, File coreLinuxDir, JSONObject nativeExecutor) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);

            // V13.3: smoke real do rootfs via PRoot, ainda com allowlist fixa.
            // Mantém o BusyBox do APK bindado no rootfs e adiciona binds do runtime
            // Android (/system, /apex e nativeLibraryDir) para o linker Bionic iniciar o ELF.
            // Esta etapa prova entrada no rootfs e execução de comandos mínimos
            // pré-definidos. Continua sem shell livre, sem comando remoto arbitrário,
            // sem Box64, sem Bedrock e sem processo persistente.
            JSONObject executor = nativeExecutor;
            if (executor == null || !executor.optBoolean("readyForRootfs", false)) {
                executor = CoreWorkerNativeExecutor.snapshot(context, layout.core, "test");
            }
            JSONObject rootfs = rootfsSnapshot(context, layout.core, "status");
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState == null) rootfsState = rootfs;
            JSONObject runtime = runtimeSnapshot(context, layout.core, "rootfs_smoke_v13_3", executor);
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(context, layout.core, "rootfs_smoke_v13_3");
            JSONObject baseSmoke = readJson(new File(layout.runtime, "core-linux-smoke-test.json"));
            if (baseSmoke == null) baseSmoke = new JSONObject();

            boolean rootfsReady = rootfsState.optBoolean("rootfsReady", false);
            String validationLevel = rootfsState.optString("validationLevel", "");
            String rootfsStateName = rootfsState.optString("state", "");
            boolean rootfsReal = runner.optBoolean("rootfsRealValidated", false)
                    || (rootfsReady && "real".equals(validationLevel))
                    || "rootfs_real_validated".equals(rootfsStateName)
                    || "rootfs_validated".equals(rootfsStateName);

            boolean baseToolsSmokeCachePresent = baseSmoke.length() > 0;
            boolean baseToolsSmokeCacheOk = baseSmoke.optBoolean("ok", false)
                    && "base_tools_smoke_ok".equals(baseSmoke.optString("state", ""));
            boolean runnerBaseRequirementsReady = runner.optBoolean("runnerBaseRequirementsReady", false)
                    || runner.optBoolean("runnerRequirementsReady", false);
            boolean baseToolsReadyFromPreflight = runner.optBoolean("baseToolsReady", false)
                    && runnerBaseRequirementsReady
                    && runner.optBoolean("prootEmbedded", false)
                    && runner.optBoolean("busyboxEmbedded", false)
                    && runner.optBoolean("prootDependencyReady", true)
                    && runner.optBoolean("busyboxDependencyReady", true);
            boolean baseToolsOk = baseToolsSmokeCacheOk || baseToolsReadyFromPreflight;
            boolean executorReady = executor.optBoolean("readyForRootfs", false);
            boolean runtimeOk = runtime.optBoolean("ok", false);
            JSONObject readiness = new JSONObject()
                    .put("stage", "core-linux-rootfs-proot-smoke-v13.3")
                    .put("baseToolsSmokeCachePresent", baseToolsSmokeCachePresent)
                    .put("baseToolsSmokeCacheOk", baseToolsSmokeCacheOk)
                    .put("baseToolsReadyFromPreflight", baseToolsReadyFromPreflight)
                    .put("baseToolsReady", runner.optBoolean("baseToolsReady", false))
                    .put("runnerBaseRequirementsReady", runnerBaseRequirementsReady)
                    .put("rootfsReady", rootfsReady)
                    .put("validationLevel", validationLevel)
                    .put("rootfsRealValidated", rootfsReal)
                    .put("executorReady", executorReady)
                    .put("runtimeOk", runtimeOk)
                    .put("prootEmbedded", runner.optBoolean("prootEmbedded", false))
                    .put("busyboxEmbedded", runner.optBoolean("busyboxEmbedded", false))
                    .put("prootDependencyReady", runner.optBoolean("prootDependencyReady", false))
                    .put("busyboxDependencyReady", runner.optBoolean("busyboxDependencyReady", false));

            JSONArray checks = new JSONArray()
                    .put(check("base tools ready", baseToolsOk,
                            baseToolsOk
                                    ? (baseToolsSmokeCacheOk ? "cache V12.3 ok" : "preflight recalculado prova BusyBox/PRoot prontos")
                                    : "base tools não prontos; rode smoke BusyBox/PRoot ou corrija preflight"))
                    .put(check("rootfs real", rootfsReal, rootfsState.optString("summary", "rootfs real precisa estar validado")))
                    .put(check("executor JNI allowlist", executorReady, executor.optString("summary", "")))
                    .put(check("runtime state", runtimeOk, runtime.optString("summary", "")))
                    .put(check("PRoot embutido", runner.optBoolean("prootEmbedded", false), runner.optString("summary", "")))
                    .put(check("BusyBox embutido", runner.optBoolean("busyboxEmbedded", false), runner.optString("summary", "")))
                    .put(check("shell livre bloqueado", true, "somente BusyBox embutido com comandos fixos allowlist"))
                    .put(check("Box64 bloqueado", true, "Box64 fica para etapa posterior"))
                    .put(check("Bedrock bloqueado", true, "nenhum servidor é iniciado nesta etapa"));

            boolean prerequisitesOk = true;
            for (int i = 0; i < checks.length(); i++) {
                JSONObject row = checks.optJSONObject(i);
                if (row != null && !row.optBoolean("ok", false)) prerequisitesOk = false;
            }

            File nativeDir = new File(context.getApplicationInfo().nativeLibraryDir == null ? "" : context.getApplicationInfo().nativeLibraryDir);
            File proot = new File(nativeDir, "libcoreworker_proot.so");
            File busyboxReal = new File(nativeDir, "libcoreworker_busybox.so");
            JSONObject busyboxLaunch = prepareBusyboxArgv0Launcher(layout.runtime, busyboxReal);
            File busyboxHost = new File(busyboxLaunch.optString("path", path(busyboxReal)));
            JSONObject rootfsBind = prepareRootfsBusyboxBind(layout.rootfs, nativeDir, busyboxHost);
            checks.put(check("BusyBox bind no rootfs", rootfsBind.optBoolean("ok", false),
                    rootfsBind.optBoolean("ok", false)
                            ? "BusyBox validado do APK será montado em /bin/busybox"
                            : rootfsBind.optString("error", "falha preparando bind do BusyBox")));
            if (!rootfsBind.optBoolean("ok", false)) prerequisitesOk = false;
            JSONObject rootfsSmoke = new JSONObject();
            JSONArray smokeChecks = new JSONArray();
            boolean echoOk = false;
            boolean osReleaseOk = false;
            boolean busyboxOk = false;

            if (prerequisitesOk) {
                String busyboxBind = rootfsBind.optString("busyboxBind", path(busyboxHost) + ":/bin/busybox");
                String nativeDirBind = rootfsBind.optString("nativeDirBind", path(nativeDir) + ":" + path(nativeDir));
                String systemBind = rootfsBind.optString("systemBind", "/system:/system");
                String apexBind = rootfsBind.optString("apexBind", "/apex:/apex");
                JSONObject echo = runNativeTool(
                        "proot rootfs busybox echo ok",
                        proot,
                        nativeDir,
                        layout.runtime,
                        10000L,
                        "-r", path(layout.rootfs), "-w", "/",
                        "-b", systemBind,
                        "-b", apexBind,
                        "-b", busyboxBind,
                        "-b", nativeDirBind,
                        "/bin/busybox", "echo", "ok"
                );
                JSONObject osRelease = runNativeTool(
                        "proot rootfs busybox cat /etc/os-release",
                        proot,
                        nativeDir,
                        layout.runtime,
                        10000L,
                        "-r", path(layout.rootfs), "-w", "/",
                        "-b", systemBind,
                        "-b", apexBind,
                        "-b", busyboxBind,
                        "-b", nativeDirBind,
                        "/bin/busybox", "cat", "/etc/os-release"
                );
                JSONObject busybox = runNativeTool(
                        "proot rootfs busybox true",
                        proot,
                        nativeDir,
                        layout.runtime,
                        10000L,
                        "-r", path(layout.rootfs), "-w", "/",
                        "-b", systemBind,
                        "-b", apexBind,
                        "-b", busyboxBind,
                        "-b", nativeDirBind,
                        "/bin/busybox", "true"
                );
                echoOk = echo.optBoolean("ok", false) && "ok".equals(echo.optString("stdout", "").trim());
                osReleaseOk = osRelease.optBoolean("ok", false) && osRelease.optString("stdout", "").contains("NAME=");
                busyboxOk = busybox.optBoolean("ok", false);
                rootfsSmoke.put("echo", echo);
                rootfsSmoke.put("osRelease", osRelease);
                rootfsSmoke.put("busyboxTrue", busybox);
                rootfsSmoke.put("bindSetup", rootfsBind);
                rootfsSmoke.put("busyboxArgv0Mode", busyboxLaunch.optString("mode", "argv0-symlink"));
                rootfsSmoke.put("busyboxHostPath", path(busyboxHost));
                rootfsSmoke.put("guestBusybox", "/bin/busybox");
                rootfsSmoke.put("nativeLibraryDir", path(nativeDir));
                rootfsSmoke.put("systemBind", systemBind);
                rootfsSmoke.put("apexBind", apexBind);
                rootfsSmoke.put("nativeDirBind", nativeDirBind);
                rootfsSmoke.put("rootfsDir", path(layout.rootfs));
                rootfsSmoke.put("workDir", path(layout.runtime));
                rootfsSmoke.put("env", new JSONObject()
                        .put("LD_LIBRARY_PATH", path(nativeDir))
                        .put("PROOT_LOADER", path(new File(nativeDir, "libcoreworker_proot_loader.so")))
                        .put("PROOT_LOADER_32", path(new File(nativeDir, "libcoreworker_proot_loader32.so"))));
                smokeChecks.put(check("rootfs echo", echoOk, echoOk ? "proot entrou no rootfs e BusyBox bindado executou echo ok" : "falha em /bin/busybox echo ok; ver stdout/stderr"));
                smokeChecks.put(check("rootfs os-release", osReleaseOk, osReleaseOk ? "/etc/os-release lido dentro do rootfs pelo BusyBox bindado" : "falha ao ler /etc/os-release dentro do rootfs"));
                smokeChecks.put(check("rootfs busybox", busyboxOk, busyboxOk ? "/bin/busybox true executou dentro do rootfs" : "falha em /bin/busybox true; ver stdout/stderr"));
            } else {
                rootfsSmoke.put("skipped", true);
                rootfsSmoke.put("reason", "base/rootfs ainda não está pronto para smoke V13.3");
                rootfsSmoke.put("readiness", readiness);
                smokeChecks.put(check("rootfs echo", false, "não executado: pré-requisitos pendentes"));
                smokeChecks.put(check("rootfs os-release", false, "não executado: pré-requisitos pendentes"));
                smokeChecks.put(check("rootfs busybox", false, "não executado: pré-requisitos pendentes"));
            }

            boolean ok = prerequisitesOk && echoOk && osReleaseOk && busyboxOk;
            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("type", "core_linux_rootfs_proot_smoke_test");
            out.put("state", ok ? "rootfs_proot_smoke_ok" : (prerequisitesOk ? "rootfs_proot_smoke_failed" : "rootfs_proot_smoke_blocked"));
            out.put("stage", "core-linux-rootfs-proot-smoke-v13.3");
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", false);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("runnerExecutionAllowed", false);
            out.put("commandsAllowlisted", true);
            out.put("commands", new JSONArray()
                    .put("proot -r <rootfs> -b /system:/system -b /apex:/apex -b <apk-busybox>:/bin/busybox /bin/busybox echo ok")
                    .put("proot -r <rootfs> -b /system:/system -b /apex:/apex -b <apk-busybox>:/bin/busybox /bin/busybox cat /etc/os-release")
                    .put("proot -r <rootfs> -b /system:/system -b /apex:/apex -b <apk-busybox>:/bin/busybox /bin/busybox true"));
            out.put("checks", checks);
            out.put("smokeChecks", smokeChecks);
            out.put("rootfsSmoke", rootfsSmoke);
            out.put("readiness", readiness);
            out.put("baseToolsSmoke", baseSmoke);
            out.put("nativeExecutor", executor);
            out.put("rootfs", rootfsState);
            out.put("runtime", runtime);
            out.put("runnerPreflight", runner);
            out.put("nextStep", ok ? "box64-intake" : "corrigir rootfs/PRoot antes de Box64");
            out.put("updatedAt", now());
            out.put("summary", ok
                    ? "Smoke rootfs V13.3 ok · PRoot entrou no rootfs e executou BusyBox bindado com runtime Android sem Termux"
                    : (prerequisitesOk
                        ? "Smoke rootfs V13.3 falhou · ver stdout/stderr por etapa"
                        : "Smoke rootfs V13.3 bloqueado · falta base/rootfs validado"));
            writeJson(new File(layout.runtime, "core-linux-rootfs-smoke-test.json"), out);
            appendLog(new File(layout.logs, "core-linux-rootfs-smoke-test.log"), out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_rootfs_smoke_test", exc);
        }
    }


    public static JSONObject box64IntakePreflight(Context context, File coreLinuxDir, JSONObject nativeExecutor) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);

            // V14.2.1: intake/preflight seguro do Box64 como asset controlado.
            // Box64 do pacote .deb é executável Linux/glibc, não uma shared library
            // Android/Bionic; portanto não deve ficar em jniLibs/nativeLibraryDir.
            // Esta etapa não executa Box64, não baixa binários, não inicia Bedrock
            // e não abre shell. Ela apenas audita o asset core-linux/bin/box64.
            JSONObject executor = nativeExecutor;
            if (executor == null || !executor.optBoolean("readyForRootfs", false)) {
                executor = CoreWorkerNativeExecutor.snapshot(context, layout.core, "test");
            }
            JSONObject rootfs = rootfsSnapshot(context, layout.core, "status");
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState == null) rootfsState = rootfs;
            JSONObject runtime = runtimeSnapshot(context, layout.core, "box64_intake_v14_2_1", executor);
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(context, layout.core, "box64_intake_v14_2_1");
            JSONObject rootfsSmoke = readJson(new File(layout.runtime, "core-linux-rootfs-smoke-test.json"));
            if (rootfsSmoke == null) rootfsSmoke = new JSONObject();

            File nativeDir = new File(context.getApplicationInfo().nativeLibraryDir == null ? "" : context.getApplicationInfo().nativeLibraryDir);
            String assetPath = "core-linux/bin/box64";
            JSONObject assetInfo = assetBinarySnapshot(context, assetPath, "box64", 131072L);
            File legacyOfficial = new File(nativeDir, "libcoreworker_box64.so");
            File legacyAlias = new File(nativeDir, "libbox64.so");
            JSONObject legacyOfficialInfo = binarySnapshot("libcoreworker_box64.so", legacyOfficial, 131072L);
            JSONObject legacyAliasInfo = binarySnapshot("libbox64.so", legacyAlias, 131072L);
            JSONObject selectedInfo = assetInfo.optBoolean("exists", false)
                    ? assetInfo
                    : (legacyOfficialInfo.optBoolean("exists", false) ? legacyOfficialInfo : legacyAliasInfo);

            boolean rootfsReady = rootfsState.optBoolean("rootfsReady", false);
            String validationLevel = rootfsState.optString("validationLevel", "");
            boolean rootfsReal = runner.optBoolean("rootfsRealValidated", false)
                    || (rootfsReady && "real".equals(validationLevel))
                    || "rootfs_validated".equals(rootfsState.optString("state", ""));
            boolean rootfsSmokeCachePresent = rootfsSmoke.length() > 0;
            boolean rootfsSmokeCacheOk = rootfsSmoke.optBoolean("ok", false)
                    && "rootfs_proot_smoke_ok".equals(rootfsSmoke.optString("state", ""));
            boolean baseReady = runner.optBoolean("baseToolsReady", false)
                    && (runner.optBoolean("runnerBaseRequirementsReady", false) || runner.optBoolean("runnerRequirementsReady", false));
            boolean rootfsReadyFromPreflight = rootfsReal && baseReady && executor.optBoolean("readyForRootfs", false);
            boolean selectedPresent = selectedInfo.optBoolean("exists", false);
            boolean selectedElf64 = selectedInfo.optBoolean("elf64", false);
            boolean selectedAarch64 = selectedInfo.optBoolean("aarch64", false);
            boolean selectedSizeOk = selectedInfo.optBoolean("sizeOk", false);
            boolean selectedExecutable = selectedInfo.optBoolean("canExecute", false);
            boolean selectedAssetOnly = selectedInfo.optBoolean("assetOnly", false);
            boolean box64Ready = selectedPresent && selectedElf64 && selectedAarch64 && selectedSizeOk;

            JSONArray checks = new JSONArray()
                    .put(check("rootfs smoke V13.3 cache", rootfsSmokeCacheOk || rootfsReadyFromPreflight,
                            rootfsSmokeCacheOk
                                    ? "rootfs/PRoot/BusyBox bindado validado por cache"
                                    : (rootfsReadyFromPreflight
                                            ? "cache ausente; preflight recalculado confirma rootfs/base prontos"
                                            : "execute o smoke rootfs V13.3 ou corrija preflight antes do Box64")))
                    .put(check("rootfs real", rootfsReal, rootfsState.optString("summary", "rootfs real precisa estar validado")))
                    .put(check("base PRoot/BusyBox", baseReady, runner.optString("summary", "base Core Linux pendente")))
                    .put(check("executor JNI", executor.optBoolean("readyForRootfs", false), executor.optString("summary", "executor pendente")))
                    .put(check("Box64 asset presente", assetInfo.optBoolean("exists", false), assetInfo.optBoolean("exists", false) ? assetPath : "core-linux/bin/box64 não está embutido em assets"))
                    .put(check("Box64 ELF64", selectedElf64, selectedPresent ? "classe ELF64=" + selectedInfo.optString("elfClass", "") : "sem binário"))
                    .put(check("Box64 AArch64", selectedAarch64, selectedPresent ? "machine=" + selectedInfo.optString("machine", "") : "sem binário"))
                    .put(check("Box64 tamanho mínimo", selectedSizeOk, selectedPresent ? "size=" + selectedInfo.optLong("size", 0L) : "sem binário"))
                    .put(check("Box64 execução ainda bloqueada", true, "V14.2.1 audita asset; não executa box64 --version ainda"))
                    .put(check("Bedrock bloqueado", true, "nenhum servidor é iniciado nesta etapa"))
                    .put(check("shell livre bloqueado", true, "sem comando remoto arbitrário"));

            boolean prerequisitesOk = rootfsReadyFromPreflight;
            boolean ok = prerequisitesOk && box64Ready;
            JSONObject readiness = new JSONObject()
                    .put("stage", "core-linux-box64-intake-preflight-v14.2.1")
                    .put("rootfsSmokeCachePresent", rootfsSmokeCachePresent)
                    .put("rootfsSmokeOk", rootfsSmokeCacheOk)
                    .put("rootfsReadyFromPreflight", rootfsReadyFromPreflight)
                    .put("baseReadyFromPreflight", baseReady)
                    .put("rootfsRealValidated", rootfsReal)
                    .put("baseToolsReady", runner.optBoolean("baseToolsReady", false))
                    .put("runnerBaseRequirementsReady", runner.optBoolean("runnerBaseRequirementsReady", false))
                    .put("executorReady", executor.optBoolean("readyForRootfs", false))
                    .put("box64AssetPresent", assetInfo.optBoolean("exists", false))
                    .put("box64Present", selectedPresent)
                    .put("box64Elf64", selectedElf64)
                    .put("box64Aarch64", selectedAarch64)
                    .put("box64SizeOk", selectedSizeOk)
                    .put("box64CanExecute", selectedExecutable)
                    .put("box64AssetOnly", selectedAssetOnly)
                    .put("box64ReadyForSmoke", ok);

            JSONObject box64Info = new JSONObject()
                    .put("selected", selectedInfo)
                    .put("asset", assetInfo)
                    .put("legacyNativeOfficial", legacyOfficialInfo)
                    .put("legacyNativeAlias", legacyAliasInfo)
                    .put("expectedAssetPath", assetPath)
                    .put("expectedNames", new JSONArray().put("core-linux/bin/box64").put("box64").put("libcoreworker_box64.so"))
                    .put("nativeLibraryDir", path(nativeDir))
                    .put("storageMode", assetInfo.optBoolean("exists", false) ? "apk-asset-controlled" : "missing")
                    .put("metadataPolicy", new JSONObject()
                            .put("origin", "ryanfortner-box64-debs-prebuilt")
                            .put("upstream", "https://github.com/ptitSeb/box64")
                            .put("license", "MIT")
                            .put("licenseStatusRequired", "verified-audited|source-built|redistributable-verified")
                            .put("noAutoDownload", true)
                            .put("noRuntimeDownload", true)
                            .put("noBedrockStart", true)
                            .put("noShell", true)
                            .put("executionBlockedUntil", "box64-version-smoke-v15"));

            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("type", "core_linux_box64_intake_preflight");
            out.put("stage", "core-linux-box64-intake-preflight-v14.2.1");
            out.put("state", ok ? "box64_intake_ready" : (prerequisitesOk ? "box64_intake_missing_binary" : "box64_intake_blocked"));
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", false);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("runnerExecutionAllowed", false);
            out.put("commandsAllowlisted", true);
            out.put("executedBox64", false);
            out.put("checks", checks);
            out.put("readiness", readiness);
            out.put("box64", box64Info);
            out.put("rootfs", rootfsState);
            out.put("rootfsSmoke", rootfsSmoke);
            out.put("runnerPreflight", runner);
            out.put("runtime", runtime);
            out.put("nativeExecutor", executor);
            out.put("nextStep", ok ? "box64-version-smoke" : (prerequisitesOk ? "embutir Box64 como asset auditado em core-linux/bin/box64" : "corrigir base/rootfs antes de Box64"));
            out.put("updatedAt", now());
            out.put("summary", ok
                    ? "Box64 V14.2.1 pronto para smoke futuro · asset auditado embutido e base rootfs validada"
                    : (prerequisitesOk
                        ? "Box64 V14.2.1 pendente · falta embutir core-linux/bin/box64 auditado"
                        : "Box64 V14.2.1 bloqueado · base/rootfs ainda não está pronta"));
            writeJson(new File(layout.runtime, "core-linux-box64-intake-preflight.json"), out);
            appendLog(new File(layout.logs, "core-linux-box64-intake-preflight.log"), out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_box64_intake_preflight", exc);
        }
    }


    public static JSONObject box64VersionSmokeTest(Context context, File coreLinuxDir, JSONObject nativeExecutor) {
        try {
            Layout layout = new Layout(resolveCoreLinuxDir(context, coreLinuxDir));
            ensureBase(layout);

            // V15: primeiro smoke controlado do Box64. Ainda não inicia Bedrock,
            // não executa binário x86_64 arbitrário, não abre shell livre e não
            // aceita comando remoto. O único executável testado é o asset auditado
            // core-linux/bin/box64, extraído para diretório privado e validado por
            // sha256/ELF/AArch64 antes de qualquer tentativa de execução.
            JSONObject executor = nativeExecutor;
            if (executor == null || !executor.optBoolean("readyForRootfs", false)) {
                executor = CoreWorkerNativeExecutor.snapshot(context, layout.core, "test");
            }
            JSONObject intake = box64IntakePreflight(context, layout.core, executor);
            JSONObject rootfs = rootfsSnapshot(context, layout.core, "status");
            JSONObject rootfsState = rootfs.optJSONObject("rootfs");
            if (rootfsState == null) rootfsState = rootfs;
            JSONObject runner = CoreLinuxRunnerPreflightManager.preflight(context, layout.core, "box64_smoke_v15");
            JSONObject runtime = runtimeSnapshot(context, layout.core, "box64_smoke_v15", executor);

            File nativeDir = new File(context.getApplicationInfo().nativeLibraryDir == null ? "" : context.getApplicationInfo().nativeLibraryDir);
            File proot = new File(nativeDir, "libcoreworker_proot.so");
            String assetPath = "core-linux/bin/box64";
            String expectedSha = "bae41f0619e51307f6e75e1d83b54137c5ba395ba46ba4394de264613bcd73ca";
            JSONObject extract = prepareBox64AssetExecutable(context, layout, assetPath, expectedSha, 131072L);
            File box64Host = new File(extract.optString("path", path(new File(layout.bin, "box64"))));
            JSONObject glibc = glibcRuntimeSnapshot(layout.rootfs);
            JSONObject mount = prepareBox64GuestMountPoint(layout.rootfs, box64Host);

            boolean rootfsReady = rootfsState.optBoolean("rootfsReady", false);
            String validationLevel = rootfsState.optString("validationLevel", "");
            boolean rootfsReal = runner.optBoolean("rootfsRealValidated", false)
                    || (rootfsReady && "real".equals(validationLevel))
                    || "rootfs_real_validated".equals(rootfsState.optString("state", ""))
                    || "rootfs_validated".equals(rootfsState.optString("state", ""));
            boolean baseReady = runner.optBoolean("baseToolsReady", false)
                    && (runner.optBoolean("runnerBaseRequirementsReady", false) || runner.optBoolean("runnerRequirementsReady", false));
            boolean intakeReady = intake.optBoolean("ok", false)
                    && "box64_intake_ready".equals(intake.optString("state", ""));
            boolean extractionReady = extract.optBoolean("ok", false)
                    && extract.optBoolean("sha256Ok", false)
                    && extract.optBoolean("elf64", false)
                    && extract.optBoolean("aarch64", false)
                    && extract.optBoolean("canExecute", false);
            boolean glibcReady = glibc.optBoolean("ok", false);
            boolean prerequisitesOk = rootfsReal && baseReady && executor.optBoolean("readyForRootfs", false) && intakeReady && extractionReady;
            boolean canRun = prerequisitesOk && glibcReady && mount.optBoolean("ok", false) && proot.exists() && proot.canExecute();

            JSONArray checks = new JSONArray()
                    .put(check("Box64 intake V14.2.1", intakeReady, intake.optString("summary", "execute o preflight Box64 antes do smoke")))
                    .put(check("rootfs real", rootfsReal, rootfsState.optString("summary", "rootfs precisa estar validado")))
                    .put(check("base PRoot/BusyBox", baseReady, runner.optString("summary", "base Core Linux pendente")))
                    .put(check("executor JNI", executor.optBoolean("readyForRootfs", false), executor.optString("summary", "executor pendente")))
                    .put(check("Box64 asset extraído", extractionReady, extract.optBoolean("ok", false) ? "asset extraído e auditado" : extract.optString("error", "asset ausente ou inválido")))
                    .put(check("runtime glibc do rootfs", glibcReady, glibcReady ? "loader/libs glibc encontrados no rootfs" : "faltam loader/libs glibc no rootfs"))
                    .put(check("PRoot disponível", proot.exists() && proot.canExecute(), path(proot)))
                    .put(check("mountpoint Box64", mount.optBoolean("ok", false), mount.optString("summary", "mountpoint pendente")))
                    .put(check("Bedrock bloqueado", true, "nenhum servidor é iniciado no V15"))
                    .put(check("shell livre bloqueado", true, "somente box64 --version/--help via allowlist fixa"))
                    .put(check("x86_64 arbitrário bloqueado", true, "nenhum binário x86_64 do usuário é executado"));

            JSONObject smoke = new JSONObject();
            JSONArray smokeChecks = new JSONArray();
            boolean versionOk = false;
            boolean helpOk = false;
            if (canRun) {
                JSONObject version = runNativeTool("proot rootfs box64 --version", proot, nativeDir, layout.runtime, 12000L,
                        "-r", path(layout.rootfs),
                        "-b", path(box64Host) + ":/usr/local/bin/box64",
                        "-w", "/",
                        "/usr/local/bin/box64", "--version");
                JSONObject help = runNativeTool("proot rootfs box64 --help", proot, nativeDir, layout.runtime, 12000L,
                        "-r", path(layout.rootfs),
                        "-b", path(box64Host) + ":/usr/local/bin/box64",
                        "-w", "/",
                        "/usr/local/bin/box64", "--help");
                smoke.put("version", version);
                smoke.put("help", help);
                versionOk = version.optBoolean("ok", false);
                helpOk = help.optBoolean("ok", false);
                smokeChecks.put(check("box64 --version", versionOk, versionOk ? "Box64 respondeu versão" : "falha no --version; ver stdout/stderr"));
                smokeChecks.put(check("box64 --help", helpOk, helpOk ? "Box64 respondeu help" : "falha no --help; ver stdout/stderr"));
            } else {
                smoke.put("skipped", true);
                smoke.put("reason", !prerequisitesOk
                        ? "preflight/extração/base ainda pendente"
                        : (!glibcReady ? "rootfs sem runtime glibc necessário ao Box64" : "mountpoint/PRoot pendente"));
                smokeChecks.put(check("box64 --version", false, "não executado: " + smoke.optString("reason")));
                smokeChecks.put(check("box64 --help", false, "não executado: " + smoke.optString("reason")));
            }

            String state;
            boolean ok = canRun && versionOk && helpOk;
            if (ok) {
                state = "box64_smoke_ok";
            } else if (!prerequisitesOk) {
                state = "box64_smoke_blocked_preflight";
            } else if (!glibcReady) {
                state = "box64_smoke_blocked_missing_glibc_runtime";
            } else {
                state = "box64_smoke_failed";
            }

            JSONObject readiness = new JSONObject()
                    .put("stage", "core-linux-box64-version-smoke-v15")
                    .put("rootfsRealValidated", rootfsReal)
                    .put("baseReadyFromPreflight", baseReady)
                    .put("executorReady", executor.optBoolean("readyForRootfs", false))
                    .put("box64IntakeReady", intakeReady)
                    .put("box64Extracted", extract.optBoolean("exists", false))
                    .put("box64Sha256Ok", extract.optBoolean("sha256Ok", false))
                    .put("box64Elf64", extract.optBoolean("elf64", false))
                    .put("box64Aarch64", extract.optBoolean("aarch64", false))
                    .put("box64CanExecute", extract.optBoolean("canExecute", false))
                    .put("glibcRuntimeReady", glibcReady)
                    .put("prootReady", proot.exists() && proot.canExecute())
                    .put("mountReady", mount.optBoolean("ok", false))
                    .put("readyForSmoke", canRun);

            JSONObject out = new JSONObject();
            out.put("ok", ok);
            out.put("type", "core_linux_box64_version_smoke");
            out.put("stage", "core-linux-box64-version-smoke-v15");
            out.put("state", state);
            out.put("termuxTouched", false);
            out.put("pythonTouched", false);
            out.put("serviceStarted", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", canRun);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("runnerExecutionAllowed", false);
            out.put("x86_64UserBinaryAllowed", false);
            out.put("commandsAllowlisted", true);
            out.put("commands", new JSONArray()
                    .put("proot -r <rootfs> -b <box64>:/usr/local/bin/box64 /usr/local/bin/box64 --version")
                    .put("proot -r <rootfs> -b <box64>:/usr/local/bin/box64 /usr/local/bin/box64 --help"));
            out.put("checks", checks);
            out.put("smokeChecks", smokeChecks);
            out.put("readiness", readiness);
            out.put("box64Extraction", extract);
            out.put("glibcRuntime", glibc);
            out.put("box64Mount", mount);
            out.put("box64Smoke", smoke);
            out.put("box64Intake", intake);
            out.put("rootfs", rootfsState);
            out.put("runnerPreflight", runner);
            out.put("runtime", runtime);
            out.put("nativeExecutor", executor);
            out.put("nextStep", ok ? "bedrock-preflight-sem-start" : (!glibcReady ? "importar rootfs Linux com glibc arm64" : "corrigir extração/runtime Box64 antes do Bedrock"));
            out.put("updatedAt", now());
            out.put("summary", ok
                    ? "Box64 V15 ok · --version/--help executaram dentro do rootfs via PRoot sem Termux"
                    : (!prerequisitesOk
                        ? "Box64 V15 bloqueado · preflight/extração/base pendente"
                        : (!glibcReady
                            ? "Box64 V15 bloqueado · rootfs sem runtime glibc arm64 necessário"
                            : "Box64 V15 falhou · ver stdout/stderr do --version/--help")));
            writeJson(new File(layout.runtime, "core-linux-box64-smoke-test.json"), out);
            appendLog(new File(layout.logs, "core-linux-box64-smoke-test.log"), out.optString("summary"));
            return out;
        } catch (Throwable exc) {
            return error("core_linux_box64_version_smoke", exc);
        }
    }

    private static JSONObject assetBinarySnapshot(Context context, String assetPath, String label, long minBytes) {
        JSONObject out = new JSONObject();
        try {
            out.put("label", clean(label, 80));
            out.put("path", "asset://" + clean(assetPath, 220));
            out.put("name", new File(assetPath == null ? "" : assetPath).getName());
            out.put("assetPath", clean(assetPath, 220));
            out.put("assetOnly", true);
            out.put("canExecute", false);
            out.put("executionPathReady", false);
            out.put("requiresExtractionForExecution", true);

            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] header = new byte[64];
            int headerLen = 0;
            long size = 0L;
            try (InputStream in = context.getAssets().open(assetPath)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) {
                    if (headerLen < header.length) {
                        int copy = Math.min(n, header.length - headerLen);
                        System.arraycopy(buf, 0, header, headerLen, copy);
                        headerLen += copy;
                    }
                    digest.update(buf, 0, n);
                    size += n;
                }
            }
            byte[] realHeader = Arrays.copyOf(header, headerLen);
            boolean isElf = realHeader.length >= 20 && realHeader[0] == 0x7f && realHeader[1] == 'E' && realHeader[2] == 'L' && realHeader[3] == 'F';
            int elfClass = realHeader.length > 4 ? (realHeader[4] & 0xff) : 0;
            int machine = realHeader.length > 19 ? ((realHeader[18] & 0xff) | ((realHeader[19] & 0xff) << 8)) : 0;
            byte[] hash = digest.digest();
            StringBuilder sb = new StringBuilder(hash.length * 2);
            for (byte b : hash) sb.append(String.format("%02x", b & 0xff));

            out.put("exists", true);
            out.put("size", size);
            out.put("sizeOk", size >= Math.max(1L, minBytes));
            out.put("isElf", isElf);
            out.put("elfClass", elfClass);
            out.put("elf64", isElf && elfClass == 2);
            out.put("machine", machine);
            out.put("aarch64", isElf && machine == 183);
            out.put("sha256", sb.toString());
        } catch (Throwable exc) {
            try {
                out.put("exists", false);
                out.put("size", 0L);
                out.put("sizeOk", false);
                out.put("isElf", false);
                out.put("elf64", false);
                out.put("aarch64", false);
                out.put("sha256", "");
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
        }
        return out;
    }

    private static JSONObject binarySnapshot(String label, File file, long minBytes) {
        JSONObject out = new JSONObject();
        try {
            out.put("label", clean(label, 80));
            out.put("path", path(file));
            boolean exists = file != null && file.exists();
            out.put("exists", exists);
            out.put("size", exists ? file.length() : 0L);
            out.put("canExecute", exists && file.canExecute());
            out.put("sizeOk", exists && file.length() >= Math.max(1L, minBytes));
            if (exists) {
                byte[] header = readBytes(file, 64);
                boolean isElf = header.length >= 20 && header[0] == 0x7f && header[1] == 'E' && header[2] == 'L' && header[3] == 'F';
                int elfClass = header.length > 4 ? (header[4] & 0xff) : 0;
                int machine = header.length > 19 ? ((header[18] & 0xff) | ((header[19] & 0xff) << 8)) : 0;
                out.put("isElf", isElf);
                out.put("elfClass", elfClass);
                out.put("elf64", isElf && elfClass == 2);
                out.put("machine", machine);
                out.put("aarch64", isElf && machine == 183);
                out.put("sha256", sha256(file));
            } else {
                out.put("isElf", false);
                out.put("elf64", false);
                out.put("aarch64", false);
                out.put("sha256", "");
            }
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
        }
        return out;
    }

    private static String sha256(File file) {
        try (FileInputStream in = new FileInputStream(file)) {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) digest.update(buf, 0, n);
            byte[] hash = digest.digest();
            StringBuilder sb = new StringBuilder(hash.length * 2);
            for (byte b : hash) sb.append(String.format("%02x", b & 0xff));
            return sb.toString();
        } catch (Throwable ignored) {
            return "";
        }
    }



    private static JSONObject prepareBox64AssetExecutable(Context context, Layout layout, String assetPath, String expectedSha256, long minBytes) {
        JSONObject out = new JSONObject();
        File dest = new File(layout.bin, "box64");
        try {
            layout.bin.mkdirs();
            JSONObject asset = assetBinarySnapshot(context, assetPath, "box64", minBytes);
            out.put("asset", asset);
            out.put("assetPath", assetPath);
            out.put("path", path(dest));
            if (!asset.optBoolean("exists", false)) {
                out.put("ok", false);
                out.put("exists", false);
                out.put("error", "asset core-linux/bin/box64 ausente no APK");
                return out;
            }
            boolean shouldCopy = !dest.exists() || dest.length() != asset.optLong("size", -1L);
            if (!shouldCopy && expectedSha256 != null && expectedSha256.length() == 64) {
                shouldCopy = !expectedSha256.equalsIgnoreCase(sha256(dest));
            }
            if (shouldCopy) {
                File parent = dest.getParentFile();
                if (parent != null) parent.mkdirs();
                File tmp = new File(parent == null ? layout.bin : parent, "box64.tmp");
                try (InputStream in = context.getAssets().open(assetPath); FileOutputStream fos = new FileOutputStream(tmp)) {
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) fos.write(buf, 0, n);
                }
                if (dest.exists()) Files.deleteIfExists(dest.toPath());
                if (!tmp.renameTo(dest)) {
                    copyFile(tmp, dest);
                    Files.deleteIfExists(tmp.toPath());
                }
            }
            //noinspection ResultOfMethodCallIgnored
            dest.setReadable(true, true);
            //noinspection ResultOfMethodCallIgnored
            dest.setWritable(true, true);
            //noinspection ResultOfMethodCallIgnored
            dest.setExecutable(true, true);
            JSONObject file = binarySnapshot("box64", dest, minBytes);
            boolean shaOk = expectedSha256 == null || expectedSha256.isEmpty() || expectedSha256.equalsIgnoreCase(file.optString("sha256", ""));
            boolean ok = file.optBoolean("exists", false)
                    && file.optBoolean("sizeOk", false)
                    && file.optBoolean("elf64", false)
                    && file.optBoolean("aarch64", false)
                    && file.optBoolean("canExecute", false)
                    && shaOk;
            out.put("ok", ok);
            out.put("exists", file.optBoolean("exists", false));
            out.put("copied", shouldCopy);
            out.put("size", file.optLong("size", 0L));
            out.put("sizeOk", file.optBoolean("sizeOk", false));
            out.put("canExecute", file.optBoolean("canExecute", false));
            out.put("elf64", file.optBoolean("elf64", false));
            out.put("aarch64", file.optBoolean("aarch64", false));
            out.put("sha256", file.optString("sha256", ""));
            out.put("expectedSha256", expectedSha256 == null ? "" : expectedSha256);
            out.put("sha256Ok", shaOk);
            out.put("file", file);
            out.put("note", "Box64 é asset Linux/glibc extraído para diretório privado antes do smoke; execução só via allowlist V15");
            if (!ok) out.put("error", "Box64 extraído não passou em sha256/ELF/AArch64/permissão");
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("exists", dest.exists());
                out.put("path", path(dest));
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static void copyFile(File src, File dst) throws Exception {
        File parent = dst.getParentFile();
        if (parent != null) parent.mkdirs();
        try (FileInputStream in = new FileInputStream(src); FileOutputStream out = new FileOutputStream(dst)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        }
    }

    private static JSONObject prepareBox64GuestMountPoint(File rootfsDir, File box64Host) {
        JSONObject out = new JSONObject();
        try {
            File usrLocalBin = new File(rootfsDir, "usr/local/bin");
            usrLocalBin.mkdirs();
            File guest = new File(usrLocalBin, "box64");
            if (!guest.exists()) {
                try (FileOutputStream fos = new FileOutputStream(guest)) {
                    fos.write(new byte[0]);
                }
            }
            //noinspection ResultOfMethodCallIgnored
            guest.setReadable(true, true);
            //noinspection ResultOfMethodCallIgnored
            guest.setExecutable(true, true);
            boolean ok = box64Host != null && box64Host.exists() && box64Host.canExecute() && guest.exists();
            out.put("ok", ok);
            out.put("mode", "bind-extracted-box64-into-rootfs");
            out.put("box64HostPath", path(box64Host));
            out.put("box64GuestPath", "/usr/local/bin/box64");
            out.put("box64MountPoint", path(guest));
            out.put("box64Bind", path(box64Host) + ":/usr/local/bin/box64");
            out.put("summary", ok ? "mountpoint Box64 pronto" : "Box64 host/mountpoint pendente");
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("mode", "bind-extracted-box64-into-rootfs");
                out.put("box64HostPath", path(box64Host));
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static JSONObject glibcRuntimeSnapshot(File rootfsDir) {
        JSONObject out = new JSONObject();
        try {
            JSONObject loader = fileStatusRelative(rootfsDir, "lib/ld-linux-aarch64.so.1");
            JSONObject libc = firstExistingRelative(rootfsDir, "lib/aarch64-linux-gnu/libc.so.6", "usr/lib/aarch64-linux-gnu/libc.so.6", "lib/libc.so.6");
            JSONObject libm = firstExistingRelative(rootfsDir, "lib/aarch64-linux-gnu/libm.so.6", "usr/lib/aarch64-linux-gnu/libm.so.6", "lib/libm.so.6");
            JSONObject libresolv = firstExistingRelative(rootfsDir, "lib/aarch64-linux-gnu/libresolv.so.2", "usr/lib/aarch64-linux-gnu/libresolv.so.2", "lib/libresolv.so.2");
            boolean ok = loader.optBoolean("exists", false)
                    && libc.optBoolean("exists", false)
                    && libm.optBoolean("exists", false)
                    && libresolv.optBoolean("exists", false);
            out.put("ok", ok);
            out.put("rootfsDir", path(rootfsDir));
            out.put("loader", loader);
            out.put("libc", libc);
            out.put("libm", libm);
            out.put("libresolv", libresolv);
            out.put("required", new JSONArray()
                    .put("/lib/ld-linux-aarch64.so.1")
                    .put("libc.so.6")
                    .put("libm.so.6")
                    .put("libresolv.so.2"));
            out.put("summary", ok ? "runtime glibc arm64 presente no rootfs" : "runtime glibc arm64 ausente/incompleto no rootfs");
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static JSONObject firstExistingRelative(File rootfsDir, String... rels) {
        JSONArray checked = new JSONArray();
        JSONObject first = new JSONObject();
        try {
            for (String rel : rels) {
                JSONObject item = fileStatusRelative(rootfsDir, rel);
                checked.put(item);
                if (item.optBoolean("exists", false)) {
                    item.put("checked", checked);
                    return item;
                }
                if (first.length() == 0) first = item;
            }
            first.put("checked", checked);
        } catch (Throwable exc) {
            try { first.put("error", shortThrowable(exc)); } catch (Throwable ignored) {}
        }
        return first;
    }

    private static JSONObject fileStatusRelative(File rootfsDir, String rel) {
        JSONObject out = new JSONObject();
        try {
            String cleanRel = stripLeadingSlash(rel == null ? "" : rel);
            File f = new File(rootfsDir, cleanRel);
            out.put("relativePath", cleanRel);
            out.put("guestPath", "/" + cleanRel);
            out.put("path", path(f));
            out.put("exists", f.exists());
            out.put("isFile", f.isFile());
            out.put("isDirectory", f.isDirectory());
            out.put("canRead", f.canRead());
            out.put("canExecute", f.canExecute());
            out.put("size", f.exists() && f.isFile() ? f.length() : 0L);
        } catch (Throwable exc) {
            try {
                out.put("relativePath", rel == null ? "" : rel);
                out.put("exists", false);
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
        }
        return out;
    }

    private static JSONObject prepareBusyboxArgv0Launcher(File runtimeDir, File busyboxReal) {
        JSONObject out = new JSONObject();
        try {
            File linksDir = new File(runtimeDir, "tool-links");
            linksDir.mkdirs();
            File link = new File(linksDir, "busybox");
            Files.deleteIfExists(link.toPath());
            Files.createSymbolicLink(link.toPath(), busyboxReal.toPath());
            //noinspection ResultOfMethodCallIgnored
            link.setExecutable(true, true);
            out.put("ok", true);
            out.put("mode", "argv0-symlink");
            out.put("path", path(link));
            out.put("target", path(busyboxReal));
            out.put("basename", link.getName());
            out.put("note", "BusyBox multi-call precisa argv0=busybox; symlink aponta para nativeLibraryDir");
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("mode", "direct-fallback");
                out.put("path", path(busyboxReal));
                out.put("target", path(busyboxReal));
                out.put("error", shortThrowable(exc));
                out.put("note", "fallback direto pode falhar com applet not found se argv0 não for busybox");
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static String stripLeadingSlash(String value) {
        if (value == null) return "";
        String out = value;
        while (out.startsWith("/")) out = out.substring(1);
        return out;
    }

    private static JSONObject prepareRootfsBusyboxBind(File rootfsDir, File nativeDir, File busyboxHost) {
        JSONObject out = new JSONObject();
        try {
            File binDir = new File(rootfsDir, "bin");
            binDir.mkdirs();
            File guestBusyboxMountPoint = new File(binDir, "busybox");
            if (!guestBusyboxMountPoint.exists()) {
                try (FileOutputStream fos = new FileOutputStream(guestBusyboxMountPoint)) {
                    fos.write(new byte[0]);
                }
            }
            //noinspection ResultOfMethodCallIgnored
            guestBusyboxMountPoint.setReadable(true, true);
            //noinspection ResultOfMethodCallIgnored
            guestBusyboxMountPoint.setExecutable(true, true);

            String nativeDirPath = path(nativeDir);
            File guestNativeDirMountPoint = new File(rootfsDir, stripLeadingSlash(nativeDirPath));
            guestNativeDirMountPoint.mkdirs();

            File hostSystem = new File("/system");
            File hostApex = new File("/apex");
            File guestSystemMountPoint = new File(rootfsDir, "system");
            File guestApexMountPoint = new File(rootfsDir, "apex");
            guestSystemMountPoint.mkdirs();
            guestApexMountPoint.mkdirs();

            boolean systemReady = hostSystem.exists() && hostSystem.isDirectory() && guestSystemMountPoint.isDirectory();
            boolean apexReady = hostApex.exists() && hostApex.isDirectory() && guestApexMountPoint.isDirectory();

            out.put("ok", busyboxHost != null && busyboxHost.exists() && busyboxHost.canExecute() && systemReady && apexReady);
            out.put("mode", "bind-apk-busybox-into-rootfs");
            out.put("busyboxHostPath", path(busyboxHost));
            out.put("busyboxGuestPath", "/bin/busybox");
            out.put("busyboxMountPoint", path(guestBusyboxMountPoint));
            out.put("busyboxBind", path(busyboxHost) + ":/bin/busybox");
            out.put("nativeDirHostPath", nativeDirPath);
            out.put("nativeDirGuestPath", nativeDirPath);
            out.put("nativeDirMountPoint", path(guestNativeDirMountPoint));
            out.put("nativeDirBind", nativeDirPath + ":" + nativeDirPath);
            out.put("systemHostPath", path(hostSystem));
            out.put("systemGuestPath", "/system");
            out.put("systemMountPoint", path(guestSystemMountPoint));
            out.put("systemBind", "/system:/system");
            out.put("systemReady", systemReady);
            out.put("apexHostPath", path(hostApex));
            out.put("apexGuestPath", "/apex");
            out.put("apexMountPoint", path(guestApexMountPoint));
            out.put("apexBind", "/apex:/apex");
            out.put("apexReady", apexReady);
            out.put("androidRuntimeBinds", new JSONArray()
                    .put("/system:/system")
                    .put("/apex:/apex")
                    .put(nativeDirPath + ":" + nativeDirPath));
            out.put("note", "V13.3 monta BusyBox validado do APK no rootfs mínimo e expõe /system, /apex e nativeLibraryDir para o linker Bionic; não exige /bin/sh nem /bin/busybox próprios do rootfs");
            if (!out.optBoolean("ok", false)) {
                out.put("error", "BusyBox host ausente/sem execução ou runtime Android indisponível para bind");
            }
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("mode", "bind-apk-busybox-into-rootfs");
                out.put("busyboxHostPath", path(busyboxHost));
                out.put("error", shortThrowable(exc));
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static JSONObject runNativeTool(String label, File executable, File nativeDir, File workDir, long timeoutMs, String... args) {
        JSONObject out = new JSONObject();
        long started = now();
        try {
            File tmpDir = new File(workDir, "tmp");
            File prootTmp = new File(workDir, "proot-tmp");
            tmpDir.mkdirs();
            prootTmp.mkdirs();

            ArrayList<String> command = new ArrayList<>();
            command.add(path(executable));
            if (args != null) command.addAll(Arrays.asList(args));

            out.put("label", label);
            out.put("attempted", true);
            out.put("executablePath", path(executable));
            out.put("exists", executable != null && executable.exists());
            out.put("size", executable != null && executable.exists() ? executable.length() : 0L);
            out.put("canExecute", executable != null && executable.canExecute());
            out.put("nativeLibraryDir", path(nativeDir));
            out.put("timeoutMs", timeoutMs);
            out.put("command", new JSONArray(command));

            if (executable == null || !executable.exists()) {
                out.put("ok", false);
                out.put("exitCode", -1);
                out.put("error", "executable missing");
                out.put("durationMs", now() - started);
                return out;
            }

            ProcessBuilder pb = new ProcessBuilder(command);
            pb.directory(workDir);
            String existingLd = pb.environment().get("LD_LIBRARY_PATH");
            String ld = path(nativeDir);
            if (existingLd != null && !existingLd.trim().isEmpty()) ld = ld + ":" + existingLd.trim();
            pb.environment().put("LD_LIBRARY_PATH", ld);
            pb.environment().put("TMPDIR", path(tmpDir));
            pb.environment().put("PROOT_TMP_DIR", path(prootTmp));
            pb.environment().put("PROOT_LOADER", path(new File(nativeDir, "libcoreworker_proot_loader.so")));
            pb.environment().put("PROOT_LOADER_32", path(new File(nativeDir, "libcoreworker_proot_loader32.so")));

            Process process = pb.start();
            StreamCollector stdout = new StreamCollector(process.getInputStream(), TEXT_LIMIT);
            StreamCollector stderr = new StreamCollector(process.getErrorStream(), TEXT_LIMIT);
            stdout.start();
            stderr.start();
            boolean finished = process.waitFor(Math.max(1L, timeoutMs), TimeUnit.MILLISECONDS);
            if (!finished) {
                process.destroyForcibly();
            }
            stdout.join(1500L);
            stderr.join(1500L);
            int exitCode = finished ? process.exitValue() : -1;
            out.put("ok", finished && exitCode == 0);
            out.put("timedOut", !finished);
            out.put("exitCode", exitCode);
            out.put("stdout", clean(stdout.text(), TEXT_LIMIT));
            out.put("stderr", clean(stderr.text(), TEXT_LIMIT));
            if (stdout.error != null) out.put("stdoutReadError", shortThrowable(stdout.error));
            if (stderr.error != null) out.put("stderrReadError", shortThrowable(stderr.error));
            out.put("durationMs", now() - started);
            return out;
        } catch (Throwable exc) {
            try {
                out.put("ok", false);
                out.put("exitCode", -1);
                out.put("error", shortThrowable(exc));
                out.put("durationMs", now() - started);
            } catch (Throwable ignored) {}
            return out;
        }
    }

    private static JSONObject prepare(Layout layout, boolean repair) throws Exception {
        if (hasRealExistingRootfs(layout.rootfs)) {
            JSONObject state = status(layout, repair ? "repair_blocked_real_rootfs" : "prepare_blocked_real_rootfs");
            state.put("ok", true);
            state.put("state", "rootfs_real_validated");
            state.put("summary", "Rootfs real já importado; scaffold não sobrescrito");
            state.put("blockers", new JSONArray());
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        if (hasUnknownExistingRootfs(layout.rootfs)) {
            JSONObject state = status(layout, repair ? "repair_blocked_unknown_rootfs" : "prepare_blocked_unknown_rootfs");
            state.put("ok", false);
            state.put("state", "rootfs_repair_needed");
            state.put("summary", "Rootfs existente desconhecido; não sobrescrevi automaticamente");
            state.put("blockers", new JSONArray().put("rootfs existente não foi criada pelo Core Worker"));
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        removeTree(layout.staging);
        layout.staging.mkdirs();
        appendLog(layout.rootfsLog, "criando rootfs staging controlado");
        JSONObject manifest = createScaffold(layout.staging, repair ? "internal-scaffold-repair" : "internal-scaffold");
        JSONObject validation = validate(layout.staging);
        if (!validation.optBoolean("ok", false)) {
            JSONObject state = status(layout, repair ? "repair_failed" : "prepare_failed");
            state.put("ok", false);
            state.put("state", "rootfs_validation_failed");
            state.put("summary", "Rootfs staging falhou na validação");
            state.put("validation", validation);
            state.put("blockers", validation.optJSONArray("missing"));
            writeState(layout, state);
            appendLog(layout.rootfsLog, state.optString("summary"));
            return state;
        }
        if (layout.rootfs.exists()) removeTree(layout.rootfs);
        layout.rootfs.getParentFile().mkdirs();
        if (!layout.staging.renameTo(layout.rootfs)) {
            copyTree(layout.staging, layout.rootfs);
            removeTree(layout.staging);
        }
        JSONObject state = status(layout, repair ? "repair" : "prepare");
        state.put("ok", true);
        state.put("state", "rootfs_validated");
        state.put("summary", "Rootfs scaffold validado · pronto para smoke test Core Linux v1");
        state.put("manifest", manifest);
        state.put("preparedAt", now());
        writeState(layout, state);
        appendLog(layout.rootfsLog, state.optString("summary"));
        return state;
    }

    private static JSONObject status(Layout layout, String action) throws Exception {
        JSONObject validation = validate(layout.rootfs);
        boolean ok = validation.optBoolean("ok", false);
        JSONObject previous = readJson(new File(layout.runtime, "rootfs-state.json"));
        JSONObject state = previous == null ? new JSONObject() : previous;
        state.put("schema", "core-worker-rootfs-state-v1");
        state.put("ok", ok);
        state.put("action", action == null ? "status" : action);
        state.put("state", layout.rootfs.exists() ? validation.optString("state", "rootfs_validation_failed") : "rootfs_missing");
        state.put("rootfsReady", ok);
        String level = validation.optString("validationLevel", "scaffold");
        boolean distributionReady = validation.optBoolean("distributionReady", false);
        state.put("validationLevel", level);
        state.put("distributionReady", distributionReady);
        state.put("readyForBox64Install", ok);
        state.put("readyForBedrockStart", false);
        state.put("rootfsDir", path(layout.rootfs));
        state.put("stagingDir", path(layout.staging));
        state.put("freeBytes", Math.max(0L, layout.core.getUsableSpace()));
        state.put("storageOk", layout.core.getUsableSpace() <= 0L || layout.core.getUsableSpace() >= MIN_RECOMMENDED_FREE_BYTES);
        state.put("recommendedFreeBytes", MIN_RECOMMENDED_FREE_BYTES);
        state.put("manifest", validation.optJSONObject("manifest") == null ? new JSONObject() : validation.optJSONObject("manifest"));
        state.put("validation", validation);
        state.put("blockers", ok ? new JSONArray() : new JSONArray().put("real".equals(level) ? "rootfs real pendente/invalidado" : "rootfs scaffold pendente/invalidado"));
        state.put("warnings", "real".equals(level)
                ? new JSONArray().put("rootfs real validado; execução de binários importados segue bloqueada nesta etapa").put("Bedrock/Box64/shell livre continuam bloqueados")
                : new JSONArray().put("rootfs atual é scaffold controlado; Ubuntu/Box64/Bedrock ficam para etapas futuras"));
        state.put("updatedAt", now());
        state.put("summary", ok
                ? ("real".equals(level) ? "Rootfs real validado · runner real ainda bloqueado" : "Rootfs scaffold validado")
                : ("real".equals(level) ? "Rootfs real pendente · importar/validar no APK" : "Rootfs scaffold pendente · preparar/validar no APK"));
        return state;
    }

    private static JSONObject validate(File rootfs) throws Exception {
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        if (manifest == null) manifest = new JSONObject();
        JSONObject checks = new JSONObject();
        checks.put("rootfsDir", rootfs.exists() && rootfs.isDirectory());
        checks.put("readyMarker", new File(rootfs, ".core-worker-rootfs-ready").exists());
        checks.put("manifest", manifest.length() > 0);
        checks.put("manifestSchema", ROOTFS_MANIFEST_SCHEMA.equals(manifest.optString("schema", "")));
        String kind = manifest.optString("kind", "");
        boolean realRootfs = ROOTFS_REAL_KIND.equals(kind);
        boolean scaffoldRootfs = ROOTFS_KIND.equals(kind);
        checks.put("manifestKind", scaffoldRootfs || realRootfs);
        checks.put("etcOsRelease", new File(rootfs, "etc/os-release").exists());
        if (realRootfs) {
            checks.put("binOrUsrBin", new File(rootfs, "bin").exists() || new File(rootfs, "usr/bin").isDirectory());
        } else {
            checks.put("binDir", new File(rootfs, "bin").isDirectory());
            checks.put("binShMarker", new File(rootfs, "bin/sh").exists());
            checks.put("usrBinDir", new File(rootfs, "usr/bin").isDirectory());
        }
        checks.put("tmpDir", new File(rootfs, "tmp").isDirectory());
        checks.put("homeCoreDir", new File(rootfs, "home/core").isDirectory());
        checks.put("varLogDir", new File(rootfs, "var/log").isDirectory());
        checks.put("policy", new File(rootfs, "opt/core-worker/rootfs-policy.json").exists());
        JSONArray missing = new JSONArray();
        JSONArray names = checks.names();
        if (names != null) {
            for (int i = 0; i < names.length(); i++) {
                String key = names.optString(i, "");
                if (!checks.optBoolean(key, false)) missing.put(key);
            }
        }
        boolean ok = missing.length() == 0;
        JSONObject out = new JSONObject();
        out.put("ok", ok);
        out.put("rootfsReady", ok);
        out.put("state", ok ? "rootfs_validated" : "rootfs_validation_failed");
        String level = ROOTFS_REAL_KIND.equals(manifest.optString("kind", "")) ? "real" : "scaffold";
        out.put("validationLevel", level);
        out.put("distributionReady", ok && "real".equals(level));
        out.put("readyForBox64Install", ok);
        out.put("readyForBedrockStart", false);
        out.put("checks", checks);
        out.put("missing", missing);
        out.put("manifest", manifest);
        return out;
    }

    private static JSONObject createScaffold(File rootfs, String source) throws Exception {
        long ts = now();
        List<String> dirs = Arrays.asList(
                "bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "run", "opt/core-worker"
        );
        for (String dir : dirs) new File(rootfs, dir).mkdirs();
        JSONObject manifest = manifest(rootfs, ts, source);
        writeJson(new File(rootfs, ".core-worker-rootfs-manifest.json"), manifest);
        writeJson(new File(rootfs, "opt/core-worker/rootfs-policy.json"), manifest.optJSONObject("policy"));
        writeText(new File(rootfs, "etc/os-release"), "NAME=\"Core Worker Internal Rootfs Scaffold\"\nID=core-worker-rootfs\nVERSION_ID=\"0.2\"\nPRETTY_NAME=\"Core Worker Internal Rootfs Scaffold 0.2\"\n");
        writeText(new File(rootfs, "bin/sh"), "Core Worker rootfs marker. This is not an executable Android shell.\n");
        writeText(new File(rootfs, "usr/bin/env"), "Core Worker rootfs marker. This is not an executable Android binary.\n");
        writeText(new File(rootfs, "README.core-worker-rootfs.txt"), "Rootfs scaffold do Core Linux Runtime v1. Valida layout/estado sem Termux, sem shell livre e sem iniciar Bedrock.\n");
        writeText(new File(rootfs, ".core-worker-rootfs-ready"), "readyAt=" + ts + "\nkind=" + ROOTFS_KIND + "\nstage=core-linux-runtime-v1\n");
        return manifest;
    }

    private static JSONObject manifest(File rootfs, long ts, String source) throws Exception {
        JSONObject policy = new JSONObject();
        policy.put("noFreeShell", true);
        policy.put("noRemoteArbitraryCommand", true);
        policy.put("noAutoDownload", true);
        policy.put("noBedrockStart", true);
        policy.put("appSpecificStorage", true);
        policy.put("termuxFallbackOnly", true);
        policy.put("runtimeV1SmokeOnly", true);
        JSONObject layout = new JSONObject();
        for (String item : Arrays.asList("bin", "usr/bin", "etc", "tmp", "home/core", "var/log", "run", "opt/core-worker")) {
            layout.put(item, true);
        }
        return new JSONObject()
                .put("schema", ROOTFS_MANIFEST_SCHEMA)
                .put("kind", ROOTFS_KIND)
                .put("version", 2)
                .put("name", "Core Linux internal rootfs scaffold")
                .put("source", source == null ? "internal-scaffold" : source)
                .put("createdAt", ts)
                .put("updatedAt", ts)
                .put("arch", "aarch64")
                .put("distribution", "core-worker-scaffold")
                .put("distributionReady", false)
                .put("readyForBox64Install", true)
                .put("readyForBedrockStart", false)
                .put("path", path(rootfs))
                .put("policy", policy)
                .put("layout", layout)
                .put("notes", new JSONArray()
                        .put("Scaffold validado para provar runtime interno sem Termux.")
                        .put("Box64, Ubuntu real e Bedrock ficam bloqueados para patches futuros."));
    }

    private static JSONObject response(Layout layout, JSONObject state, String action) throws Exception {
        JSONObject out = new JSONObject();
        out.put("ok", state.optBoolean("ok", false));
        out.put("component", "core_linux_rootfs");
        out.put("summary", state.optString("summary", "Rootfs interno atualizado"));
        out.put("state", state.optString("state", "unknown"));
        out.put("action", action == null ? "status" : action);
        out.put("rootfsReady", state.optBoolean("rootfsReady", false));
        out.put("readyForBox64Install", state.optBoolean("readyForBox64Install", false));
        out.put("readyForBedrockStart", false);
        out.put("termuxTouched", false);
        out.put("pythonTouched", false);
        out.put("serviceStarted", false);
        out.put("rootfs", state);
        out.put("rootfsDir", path(layout.rootfs));
        out.put("statePath", path(new File(layout.runtime, "rootfs-state.json")));
        out.put("manifestPath", path(new File(layout.manifests, "rootfs-manifest.json")));
        out.put("logs", new JSONObject().put("install", path(layout.rootfsLog)).put("validate", path(layout.validateLog)));
        out.put("size", dirSize(layout.core, 900));
        out.put("validationLevel", state.optString("validationLevel", ""));
        out.put("distributionReady", state.optBoolean("distributionReady", false));
        out.put("safety", "rootfs app-specific; sem shell livre, sem executar binários importados, sem iniciar Bedrock");
        return out;
    }

    private static JSONObject check(String label, boolean ok, String detail) throws Exception {
        return new JSONObject().put("label", label).put("ok", ok).put("detail", clean(detail, 240));
    }

    private static void writeState(Layout layout, JSONObject state) throws Exception {
        writeJson(new File(layout.runtime, "rootfs-state.json"), state);
        JSONObject manifest = state.optJSONObject("manifest");
        if (manifest == null) manifest = new JSONObject();
        writeJson(new File(layout.manifests, "rootfs-manifest.json"), manifest);
    }

    private static boolean hasRealExistingRootfs(File rootfs) {
        if (rootfs == null || !rootfs.exists()) return false;
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        return manifest != null && ROOTFS_REAL_KIND.equals(manifest.optString("kind", ""));
    }

    private static boolean hasUnknownExistingRootfs(File rootfs) {
        if (!rootfs.exists()) return false;
        File[] files = rootfs.listFiles();
        if (files == null || files.length == 0) return false;
        JSONObject manifest = readJson(new File(rootfs, ".core-worker-rootfs-manifest.json"));
        if (manifest != null && (ROOTFS_KIND.equals(manifest.optString("kind", "")) || ROOTFS_REAL_KIND.equals(manifest.optString("kind", "")))) return false;
        return !(new File(rootfs, ".core-worker-rootfs-ready").exists()
                && new File(rootfs, "README.core-worker-rootfs.txt").exists());
    }

    private static File resolveCoreLinuxDir(Context context, File provided) {
        if (provided != null) return provided;
        return new File(context.getFilesDir(), "core-linux");
    }

    private static void ensureBase(Layout layout) {
        layout.core.mkdirs();
        layout.runtime.mkdirs();
        layout.logs.mkdirs();
        layout.manifests.mkdirs();
        layout.downloads.mkdirs();
        layout.bedrock.mkdirs();
        layout.bin.mkdirs();
        layout.scripts.mkdirs();
        try {
            writeJson(new File(layout.core, "runtime-marker.json"), new JSONObject()
                    .put("schema", "core-linux-runtime-marker-v1")
                    .put("stage", "core-linux-runtime-v1")
                    .put("termuxRequired", false)
                    .put("updatedAt", now()));
        } catch (Throwable ignored) {
        }
    }

    private static void copyTree(File src, File dst) throws Exception {
        if (src.isDirectory()) {
            if (!dst.exists()) dst.mkdirs();
            File[] files = src.listFiles();
            if (files != null) {
                for (File child : files) copyTree(child, new File(dst, child.getName()));
            }
        } else {
            File parent = dst.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileInputStream in = new FileInputStream(src); FileOutputStream out = new FileOutputStream(dst)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) >= 0) out.write(buf, 0, n);
            }
        }
    }

    private static void removeTree(File file) {
        if (file == null || !file.exists()) return;
        File[] files = file.listFiles();
        if (files != null) {
            Arrays.sort(files, Comparator.comparing(File::getAbsolutePath).reversed());
            for (File child : files) removeTree(child);
        }
        //noinspection ResultOfMethodCallIgnored
        file.delete();
    }

    private static JSONObject readJson(File file) {
        try {
            if (file == null || !file.exists()) return null;
            byte[] raw = readBytes(file, TEXT_LIMIT * 4);
            return new JSONObject(new String(raw, StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static byte[] readBytes(File file, int limit) throws Exception {
        try (FileInputStream in = new FileInputStream(file)) {
            byte[] buf = new byte[Math.max(1, Math.min(limit, (int) Math.max(1, file.length())) )];
            int n = in.read(buf);
            if (n <= 0) return new byte[0];
            if (n == buf.length) return buf;
            return Arrays.copyOf(buf, n);
        }
    }

    private static void writeJson(File file, JSONObject obj) throws Exception {
        writeText(file, obj == null ? "{}" : obj.toString(2));
    }

    private static void writeText(File file, String value) throws Exception {
        File parent = file.getParentFile();
        if (parent != null) parent.mkdirs();
        try (FileOutputStream out = new FileOutputStream(file, false)) {
            out.write(String.valueOf(value == null ? "" : value).getBytes(StandardCharsets.UTF_8));
        }
    }

    private static void appendLog(File file, String line) {
        try {
            File parent = file.getParentFile();
            if (parent != null) parent.mkdirs();
            try (FileOutputStream out = new FileOutputStream(file, true)) {
                String text = "[" + now() + "] " + clean(line, 1200) + "\n";
                out.write(text.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Throwable ignored) {
        }
    }

    private static JSONObject dirSize(File dir, int maxFiles) throws Exception {
        long[] acc = new long[]{0L, 0L};
        accumulate(dir, acc, Math.max(1, maxFiles));
        return new JSONObject().put("bytes", acc[0]).put("files", acc[1]).put("limited", acc[1] >= Math.max(1, maxFiles));
    }

    private static void accumulate(File f, long[] acc, int maxFiles) {
        if (f == null || !f.exists() || acc[1] >= maxFiles) return;
        if (f.isFile()) {
            acc[0] += Math.max(0L, f.length());
            acc[1] += 1L;
            return;
        }
        File[] files = f.listFiles();
        if (files == null) return;
        for (File child : files) {
            if (acc[1] >= maxFiles) break;
            accumulate(child, acc, maxFiles);
        }
    }

    private static JSONObject error(String component, Throwable exc) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", false);
            out.put("component", component);
            out.put("state", "error");
            out.put("summary", "falha no " + component + ": " + shortThrowable(exc));
            out.put("error", shortThrowable(exc));
            out.put("termuxTouched", false);
            out.put("serviceStarted", false);
        } catch (Throwable ignored) {
        }
        return out;
    }

    private static String clean(String value, int limit) {
        String text = String.valueOf(value == null ? "" : value)
                .replace((char) 0, ' ')
                .replace('\r', ' ')
                .trim();
        text = text.replaceAll("(?i)(token|authorization|bearer|secret|password|passwd)[=: ]+[^\\s]+", "$1=[redacted]");
        if (text.length() > limit) text = text.substring(0, Math.max(0, limit)) + "…";
        return text;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getMessage();
        return exc.getClass().getSimpleName() + (msg == null || msg.isEmpty() ? "" : ": " + clean(msg, 180));
    }

    private static long now() {
        return System.currentTimeMillis();
    }

    private static String path(File file) {
        try {
            return file == null ? "" : file.getAbsolutePath();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static final class StreamCollector extends Thread {
        private final InputStream in;
        private final int limit;
        private final ByteArrayOutputStream out = new ByteArrayOutputStream();
        volatile Throwable error;

        StreamCollector(InputStream in, int limit) {
            this.in = in;
            this.limit = Math.max(1024, limit);
        }

        @Override
        public void run() {
            byte[] buffer = new byte[4096];
            try {
                int n;
                while ((n = in.read(buffer)) >= 0) {
                    int remaining = limit - out.size();
                    if (remaining > 0) {
                        out.write(buffer, 0, Math.min(n, remaining));
                    }
                }
            } catch (Throwable exc) {
                error = exc;
            }
        }

        String text() {
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private static final class Layout {
        final File core;
        final File rootfs;
        final File staging;
        final File runtime;
        final File logs;
        final File manifests;
        final File downloads;
        final File bedrock;
        final File bin;
        final File scripts;
        final File rootfsLog;
        final File validateLog;

        Layout(File core) {
            this.core = core;
            this.rootfs = new File(core, "rootfs");
            this.staging = new File(new File(core, "staging"), "rootfs-next");
            this.runtime = new File(core, "runtime");
            this.logs = new File(core, "logs");
            this.manifests = new File(core, "manifests");
            this.downloads = new File(core, "downloads");
            this.bedrock = new File(core, "bedrock");
            this.bin = new File(core, "bin");
            this.scripts = new File(core, "scripts");
            this.rootfsLog = new File(logs, "rootfs-install.log");
            this.validateLog = new File(logs, "rootfs-validate.log");
        }
    }
}
