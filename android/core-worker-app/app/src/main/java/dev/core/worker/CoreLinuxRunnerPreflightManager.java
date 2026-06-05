package dev.core.worker;

import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Preflight v5 do runner Core Linux.
 *
 * Esta etapa apenas detecta requisitos e escreve estado. Ela não inicia Bedrock,
 * não executa Box64/proot/busybox, não abre shell livre e não aceita comando remoto.
 */
public final class CoreLinuxRunnerPreflightManager {
    private static final int TEXT_LIMIT = 64 * 1024;

    private CoreLinuxRunnerPreflightManager() {}

    public static JSONObject preflight(Context context, File coreLinuxDir, String action) {
        try {
            File base = resolveCoreLinuxDir(context, coreLinuxDir);
            Layout layout = new Layout(base);
            ensureBase(layout);
            String safeAction = clean(action, 80);
            if (safeAction.isEmpty()) safeAction = "status";

            JSONObject rootfsState = readJson(new File(layout.runtime, "rootfs-state.json"));
            JSONObject importState = readJson(new File(layout.runtime, "rootfs-import-state.json"));
            JSONObject nativeExecutor = CoreWorkerNativeExecutor.snapshot(context, base, "probe");
            JSONObject runnerGuard = CoreWorkerRunnerGuard.snapshot();

            ApplicationInfo info = context.getApplicationInfo();
            File nativeDir = new File(info == null || info.nativeLibraryDir == null ? "" : info.nativeLibraryDir);
            File dataDir = new File(info == null || info.dataDir == null ? context.getFilesDir().getParent() : info.dataDir);
            File rootfsDir = new File(base, "rootfs");
            File bedrockDir = new File(new File(base, "bedrock"), "server");
            File bedrockAltDir = new File(base, "bedrock");
            File bedrockServer = firstExisting(new File(bedrockDir, "bedrock_server"), new File(bedrockAltDir, "bedrock_server"));
            File properties = firstExisting(new File(bedrockDir, "server.properties"), new File(bedrockAltDir, "server.properties"));

            String[] executorNames = new String[]{"libcoreworker_executor.so"};
            String[] runnerNames = new String[]{"libcoreworker_runner.so"};
            String[] prootNames = new String[]{"libcoreworker_proot.so", "libproot.so"};
            String[] busyboxNames = new String[]{"libcoreworker_busybox.so", "libbusybox.so"};
            String[] libtallocNames = new String[]{"libcoreworker_libtalloc.so", "libtalloc.so", "libtalloc.so.2"};
            String[] box64Names = new String[]{"libcoreworker_box64.so", "libbox64.so"};
            File embeddedExecutor = firstExisting(nativeDir, executorNames);
            File embeddedRunner = firstExisting(nativeDir, runnerNames);
            File embeddedProot = firstExisting(nativeDir, prootNames);
            File embeddedBusybox = firstExisting(nativeDir, busyboxNames);
            File embeddedLibtalloc = firstExisting(nativeDir, libtallocNames);
            File embeddedBox64 = firstExisting(nativeDir, box64Names);
            JSONObject apkExecutor = apkNativeInfo(context, executorNames);
            JSONObject apkRunner = apkNativeInfo(context, runnerNames);
            JSONObject apkProot = apkNativeInfo(context, prootNames);
            JSONObject apkBusybox = apkNativeInfo(context, busyboxNames);
            JSONObject apkLibtalloc = apkNativeInfo(context, libtallocNames);
            JSONObject apkBox64 = apkNativeInfo(context, box64Names);
            File writableBox64 = firstExisting(new File(base, "bin/box64"), new File(base, "box64/box64"));
            boolean writableBox64Blocked = writableBox64 != null && Build.VERSION.SDK_INT >= 29 && isInside(writableBox64, dataDir);

            JSONObject sourcePlan = readAssetJson(context, "core-linux/embedded-binaries-source-plan.json");
            JSONObject localManifest = readAssetJson(context, "core-linux/embedded-binaries-manifest.json");

            boolean rootfsReal = isRootfsRealValidated(rootfsState, importState, rootfsDir);
            boolean executorApkReady = apkExecutor.optBoolean("present", false) && apkExecutor.optLong("size", 0L) > 0L;
            boolean nativeExecutorReady = nativeExecutor.optBoolean("readyForRootfs", false)
                    || nativeExecutor.optBoolean("embeddedExecutorPresent", false)
                    || embeddedExecutor != null
                    || executorApkReady;
            boolean runnerLoadedByJni = runnerGuard.optBoolean("loaded", false);
            boolean executorLoadedByJni = nativeExecutor.optJSONObject("nativeBridge") != null
                    && nativeExecutor.optJSONObject("nativeBridge").optBoolean("loaded", false);
            JSONObject embeddedAssets = embeddedSnapshot(nativeDir, dataDir, embeddedExecutor, embeddedRunner, embeddedProot, embeddedBusybox, embeddedLibtalloc, embeddedBox64,
                    executorNames, runnerNames, prootNames, busyboxNames, libtallocNames, box64Names,
                    executorLoadedByJni, runnerLoadedByJni, apkExecutor, apkRunner, apkProot, apkBusybox, apkLibtalloc, apkBox64,
                    localManifest, sourcePlan);
            JSONObject runnerInfo = embeddedAssets.optJSONObject("runner") == null ? new JSONObject() : embeddedAssets.optJSONObject("runner");
            JSONObject prootInfo = embeddedAssets.optJSONObject("proot") == null ? new JSONObject() : embeddedAssets.optJSONObject("proot");
            JSONObject busyboxInfo = embeddedAssets.optJSONObject("busybox") == null ? new JSONObject() : embeddedAssets.optJSONObject("busybox");
            JSONObject libtallocInfo = embeddedAssets.optJSONObject("libtalloc") == null ? new JSONObject() : embeddedAssets.optJSONObject("libtalloc");
            JSONObject box64Info = embeddedAssets.optJSONObject("box64") == null ? new JSONObject() : embeddedAssets.optJSONObject("box64");
            boolean runnerAssetReady = runnerInfo.optBoolean("allowedForFutureExecution", false);
            boolean prootReady = prootInfo.optBoolean("allowedForFutureExecution", false);
            boolean busyboxReady = busyboxInfo.optBoolean("allowedForFutureExecution", false);
            boolean libtallocReady = libtallocInfo.optBoolean("allowedForFutureExecution", false);
            boolean box64Ready = box64Info.optBoolean("allowedForFutureExecution", false);
            boolean bedrockServerReady = bedrockServer != null && bedrockServer.exists();
            boolean propertiesReady = properties != null && properties.exists();
            boolean prootNeedsLibtalloc = prootReady && metadataNeedsDependency(prootInfo, "libtalloc");
            boolean prootDependencyReady = !prootNeedsLibtalloc || libtallocReady;
            boolean baseToolsReady = prootReady && busyboxReady && prootDependencyReady;

            JSONArray checks = new JSONArray();
            JSONArray missing = new JSONArray();
            JSONArray blockers = new JSONArray();
            JSONArray warnings = new JSONArray();
            JSONArray nextActions = new JSONArray();

            addCheck(checks, missing, "rootfs_real", "Rootfs real validado", rootfsReal, "importe/valide um rootfs real antes do runner");
            addCheck(checks, missing, "native_executor", "Executor nativo do APK", nativeExecutorReady, "embutir/testar executor nativo allowlist");
            addCheck(checks, missing, "core_runner_asset", "core-runner embutido no APK", runnerAssetReady, "embutir core-runner arm64 como libcoreworker_runner.so");
            addCheck(checks, missing, "proot_embedded", "proot embutido no APK", prootReady, "embutir proot arm64 auditado com metadata aprovada");
            addCheck(checks, missing, "busybox_embedded", "busybox embutido no APK", busyboxReady, "embutir busybox arm64 auditado com metadata aprovada");
            if (prootNeedsLibtalloc) {
                addCheck(checks, missing, "libtalloc_embedded", "libtalloc embutido no APK", libtallocReady, "embutir libtalloc arm64 auditado junto com proot dinâmico");
            }
            addCheck(checks, missing, "box64_embedded", "Box64 embutido no APK", box64Ready, "embutir Box64 arm64 auditado com metadata aprovada; não usar binário baixado executável");
            addCheck(checks, missing, "bedrock_server", "bedrock_server presente", bedrockServerReady, "preparar arquivos do servidor Bedrock");
            addCheck(checks, missing, "server_properties", "server.properties presente", propertiesReady, "gerar ou revisar server.properties");

            if (writableBox64Blocked) {
                warnings.put("Box64 detectado em diretório gravável do app; Android 10+ bloqueia execução segura desse caminho");
            }
            addMetadataWarning(warnings, "proot", prootInfo);
            addMetadataWarning(warnings, "busybox", busyboxInfo);
            addMetadataWarning(warnings, "libtalloc", libtallocInfo);
            addMetadataWarning(warnings, "box64", box64Info);
            if (prootReady && prootNeedsLibtalloc && !libtallocReady) {
                warnings.put("proot parece depender de libtalloc; execução futura segue bloqueada até libtalloc auditado estar embutido");
            }
            if (Build.VERSION.SDK_INT >= 29) {
                warnings.put("execução futura deve usar componentes embutidos no APK/native libs; binários importados não são executados");
            }
            blockers.put("runner real permanece bloqueado no preflight v5");
            blockers.put("Bedrock start real permanece bloqueado");
            blockers.put("shell livre permanece bloqueado");
            blockers.put("comando remoto arbitrário permanece bloqueado");

            for (int i = 0; i < missing.length(); i++) {
                String item = missing.optString(i, "");
                if (!item.isEmpty()) nextActions.put(item);
            }
            if (nextActions.length() == 0) {
                nextActions.put("aguardar patch do runner nativo controlado; execução real ainda não liberada");
            }

            boolean requirementsReady = rootfsReal && nativeExecutorReady && runnerAssetReady && prootReady && busyboxReady && prootDependencyReady && box64Ready
                    && bedrockServerReady && propertiesReady && !writableBox64Blocked;
            boolean runnerReady = false;
            String state = requirementsReady ? "runner_preflight_ready_but_blocked" : "runner_preflight_blocked";
            String summary = requirementsReady
                    ? "Runner preflight pronto · execução real ainda bloqueada"
                    : "Runner preflight concluído · " + missing.length() + " pendência(s)";

            JSONObject out = new JSONObject();
            out.put("ok", true);
            out.put("component", "core_linux_runner_preflight");
            out.put("action", safeAction);
            out.put("stage", "core-linux-runner-preflight-v5");
            out.put("preflightVersion", 5);
            out.put("state", state);
            out.put("summary", summary);
            out.put("coreLinuxDir", path(base));
            out.put("nativeLibraryDir", path(nativeDir));
            out.put("androidSdk", Build.VERSION.SDK_INT);
            out.put("termuxRequired", false);
            out.put("pythonTouched", false);
            out.put("bedrockStarted", false);
            out.put("box64Started", false);
            out.put("shellOpened", false);
            out.put("remoteCommandAllowed", false);
            out.put("runnerReady", runnerReady);
            out.put("runnerBlocked", true);
            out.put("runnerExecutionAllowed", false);
            out.put("runnerRequirementsReady", requirementsReady);
            out.put("bedrockStartAllowed", false);
            out.put("rootfsRealValidated", rootfsReal);
            out.put("nativeExecutorReady", nativeExecutorReady);
            out.put("coreRunnerEmbedded", runnerAssetReady);
            out.put("prootEmbedded", prootReady);
            out.put("busyboxEmbedded", busyboxReady);
            out.put("libtallocEmbedded", libtallocReady);
            out.put("prootNeedsLibtalloc", prootNeedsLibtalloc);
            out.put("prootDependencyReady", prootDependencyReady);
            out.put("baseToolsReady", baseToolsReady);
            out.put("box64Embedded", box64Ready);
            out.put("writableBox64Blocked", writableBox64Blocked);
            out.put("bedrockServerPresent", bedrockServerReady);
            out.put("serverPropertiesPresent", propertiesReady);
            out.put("checks", checks);
            out.put("missing", missing);
            out.put("blockers", blockers);
            out.put("warnings", warnings);
            out.put("nextActions", nextActions);
            out.put("rootfs", compactRootfs(rootfsState, importState));
            out.put("nativeExecutor", compactNative(nativeExecutor));
            out.put("coreRunnerNativeBridge", runnerGuard);
            out.put("assetManifest", assetManifest(executorNames, runnerNames, prootNames, busyboxNames, libtallocNames, box64Names, localManifest, sourcePlan));
            out.put("embedded", embeddedAssets);
            out.put("writableCandidates", writableSnapshot(writableBox64, dataDir));
            out.put("bedrockFiles", bedrockFilesSnapshot(bedrockServer, properties));
            out.put("safety", "preflight apenas detecta requisitos; sem start real, sem shell livre, sem comando remoto, sem executar binários importados");
            out.put("updatedAt", System.currentTimeMillis());

            writeJson(new File(layout.runtime, "runner-preflight-state.json"), out);
            writeJson(new File(layout.runtime, "runner-state.json"), out);
            appendLog(new File(layout.logs, "runner-preflight.log"), summary);
            return out;
        } catch (Throwable exc) {
            JSONObject err = new JSONObject();
            try {
                err.put("ok", false);
                err.put("component", "core_linux_runner_preflight");
                err.put("state", "runner_preflight_error");
                err.put("summary", "falha no runner preflight: " + shortThrowable(exc));
                err.put("error", shortThrowable(exc));
                err.put("runnerBlocked", true);
                err.put("runnerExecutionAllowed", false);
                err.put("bedrockStartAllowed", false);
                err.put("preflightVersion", 5);
                err.put("updatedAt", System.currentTimeMillis());
            } catch (Throwable ignored) {}
            return err;
        }
    }

    public static JSONObject status(Context context, File coreLinuxDir) {
        return preflight(context, coreLinuxDir, "status");
    }

    private static void addCheck(JSONArray checks, JSONArray missing, String key, String label, boolean ok, String nextAction) throws Exception {
        checks.put(new JSONObject()
                .put("key", key)
                .put("label", label)
                .put("ok", ok)
                .put("nextAction", ok ? "" : nextAction));
        if (!ok) missing.put(label);
    }

    private static boolean isRootfsRealValidated(JSONObject rootfsState, JSONObject importState, File rootfsDir) {
        String state = lower(firstNonEmpty(
                rootfsState.optString("state", ""),
                importState.optString("state", "")));
        String level = lower(firstNonEmpty(
                rootfsState.optString("validationLevel", ""),
                rootfsState.optString("rootfsValidationLevel", ""),
                importState.optString("validationLevel", "")));
        boolean marker = new File(rootfsDir, ".core-worker-rootfs-ready").exists();
        boolean manifest = new File(rootfsDir, ".core-worker-rootfs-manifest.json").exists();
        boolean osRelease = new File(rootfsDir, "etc/os-release").exists();
        return (state.contains("rootfs_real_validated") || "real".equals(level)) && marker && manifest && osRelease;
    }

    private static JSONObject compactRootfs(JSONObject rootfsState, JSONObject importState) throws Exception {
        JSONObject out = new JSONObject();
        String summary = firstNonEmpty(rootfsState.optString("summary", ""), importState.optString("summary", ""));
        String state = firstNonEmpty(rootfsState.optString("state", ""), importState.optString("state", ""));
        out.put("state", state);
        out.put("summary", summary);
        out.put("rootfsReady", rootfsState.optBoolean("rootfsReady", importState.optBoolean("rootfsReady", false)));
        out.put("validationLevel", firstNonEmpty(rootfsState.optString("validationLevel", ""), importState.optString("validationLevel", "")));
        out.put("distributionReady", rootfsState.optBoolean("distributionReady", importState.optBoolean("distributionReady", false)));
        out.put("updatedAt", Math.max(rootfsState.optLong("updatedAt", 0L), importState.optLong("updatedAt", 0L)));
        return out;
    }

    private static JSONObject compactNative(JSONObject nativeExecutor) throws Exception {
        JSONObject out = new JSONObject();
        out.put("state", nativeExecutor.optString("state", ""));
        out.put("summary", nativeExecutor.optString("summary", ""));
        out.put("readyForRootfs", nativeExecutor.optBoolean("readyForRootfs", false));
        out.put("embeddedExecutorPresent", nativeExecutor.optBoolean("embeddedExecutorPresent", false));
        out.put("embeddedBox64Present", nativeExecutor.optBoolean("embeddedBox64Present", false));
        out.put("downloadedExecutableBlocked", nativeExecutor.optBoolean("downloadedExecutableBlocked", false));
        out.put("updatedAt", nativeExecutor.optLong("updatedAt", 0L));
        return out;
    }

    private static JSONObject assetManifest(String[] executor, String[] runner, String[] proot, String[] busybox, String[] libtalloc, String[] box64,
                                            JSONObject localManifest, JSONObject sourcePlan) throws Exception {
        return new JSONObject()
                .put("stage", "core-linux-embedded-binaries-intake-v5")
                .put("abi", "arm64-v8a")
                .put("executor", new JSONArray(executor))
                .put("runner", new JSONArray(runner))
                .put("proot", new JSONArray(proot))
                .put("busybox", new JSONArray(busybox))
                .put("libtalloc", new JSONArray(libtalloc))
                .put("box64", new JSONArray(box64))
                .put("localManifestSchema", localManifest == null ? "" : localManifest.optString("schema", ""))
                .put("sourcePlanSchema", sourcePlan == null ? "" : sourcePlan.optString("schema", ""))
                .put("policy", "somente componentes embutidos no APK/native libs ou JNI carregado pelo APK podem virar executáveis futuros; assets externos exigem metadata aprovada");
    }

    private static JSONObject embeddedSnapshot(File nativeDir, File dataDir, File executor, File runner, File proot, File busybox, File libtalloc, File box64,
                                               String[] executorNames, String[] runnerNames, String[] prootNames, String[] busyboxNames, String[] libtallocNames, String[] box64Names,
                                               boolean executorLoadedByJni, boolean runnerLoadedByJni,
                                               JSONObject apkExecutor, JSONObject apkRunner, JSONObject apkProot, JSONObject apkBusybox, JSONObject apkLibtalloc, JSONObject apkBox64,
                                               JSONObject localManifest, JSONObject sourcePlan) throws Exception {
        return new JSONObject()
                .put("nativeLibraryDir", path(nativeDir))
                .put("executor", assetInfo("executor", executor, executorNames, nativeDir, dataDir, executorLoadedByJni, "jni-loaded:coreworker_executor", apkExecutor, localManifest, sourcePlan))
                .put("runner", assetInfo("runner", runner, runnerNames, nativeDir, dataDir, runnerLoadedByJni, "jni-loaded:coreworker_runner", apkRunner, localManifest, sourcePlan))
                .put("proot", assetInfo("proot", proot, prootNames, nativeDir, dataDir, false, "", apkProot, localManifest, sourcePlan))
                .put("busybox", assetInfo("busybox", busybox, busyboxNames, nativeDir, dataDir, false, "", apkBusybox, localManifest, sourcePlan))
                .put("libtalloc", assetInfo("libtalloc", libtalloc, libtallocNames, nativeDir, dataDir, false, "", apkLibtalloc, localManifest, sourcePlan))
                .put("box64", assetInfo("box64", box64, box64Names, nativeDir, dataDir, false, "", apkBox64, localManifest, sourcePlan));
    }

    private static void addMetadataWarning(JSONArray warnings, String kind, JSONObject info) throws Exception {
        if (info == null) return;
        if (!info.optBoolean("present", false)) return;
        if (info.optBoolean("metadataApproved", false)) return;
        warnings.put(kind + " detectado, mas metadata de origem/licença ainda não está aprovada; asset não será liberado para execução futura");
    }

    private static JSONObject writableSnapshot(File box64, File dataDir) throws Exception {
        return new JSONObject().put("box64", fileInfo(box64, dataDir));
    }

    private static JSONObject bedrockFilesSnapshot(File server, File properties) throws Exception {
        return new JSONObject()
                .put("server", fileInfo(server, null))
                .put("properties", fileInfo(properties, null));
    }

    private static JSONObject assetInfo(String kind, File file, String[] expectedNames, File nativeDir, File dataDir,
                                        boolean jniLoadedFallback, String detectedByFallback, JSONObject apkEntryInfo,
                                        JSONObject localManifest, JSONObject sourcePlan) throws Exception {
        JSONObject out = fileInfo(file, dataDir);
        boolean fileEmbedded = file != null && file.exists() && nativeDir != null && isInside(file, nativeDir);
        boolean validPhysicalFile = out.optBoolean("present", false) && out.optLong("size", 0L) > 0L;
        JSONObject apkInfo = apkEntryInfo == null ? new JSONObject() : apkEntryInfo;
        boolean apkEmbedded = apkInfo.optBoolean("present", false) && apkInfo.optLong("size", 0L) > 0L;
        if (!validPhysicalFile && apkEmbedded) {
            out.put("present", true);
            out.put("path", apkInfo.optString("path", ""));
            out.put("name", apkInfo.optString("name", expectedNames != null && expectedNames.length > 0 ? expectedNames[0] : ""));
            out.put("size", apkInfo.optLong("size", 0L));
            out.put("canExecute", true);
            out.put("blockedByWritableAppHome", false);
            out.put("sha256", apkInfo.optString("sha256", ""));
            out.put("detectedBy", jniLoadedFallback && detectedByFallback != null && !detectedByFallback.isEmpty() ? detectedByFallback + "+apk-entry" : apkInfo.optString("detectedBy", "apk-native-lib-entry"));
            out.put("sourceApk", apkInfo.optString("sourceApk", ""));
            out.put("zipEntry", apkInfo.optString("zipEntry", ""));
            out.put("compressionMethod", apkInfo.optString("compressionMethod", ""));
        } else if (!validPhysicalFile && jniLoadedFallback) {
            out.put("present", true);
            out.put("path", path(nativeDir));
            out.put("name", expectedNames != null && expectedNames.length > 0 ? expectedNames[expectedNames.length - 1] : "");
            out.put("size", 0L);
            out.put("canExecute", true);
            out.put("sha256", "");
            out.put("detectedBy", detectedByFallback == null ? "jni-loaded" : detectedByFallback);
        } else {
            out.put("detectedBy", fileEmbedded ? "native-library-dir" : "missing");
        }
        out.put("kind", kind == null ? "" : kind);
        out.put("abi", "arm64-v8a");
        out.put("expectedNames", new JSONArray(expectedNames));
        JSONObject metadata = metadataFor(kind, localManifest, sourcePlan);
        out.put("metadata", metadata);
        boolean placeholder = out.optBoolean("present", false) && out.optLong("size", 0L) <= 0L && !jniLoadedFallback;
        boolean embedded = fileEmbedded || apkEmbedded || jniLoadedFallback;
        boolean metadataApproved = metadataApproved(kind, metadata);
        boolean basicAllowed = embedded
                && !out.optBoolean("blockedByWritableAppHome", false)
                && !placeholder
                && (out.optLong("size", 0L) > 0L || jniLoadedFallback);
        out.put("embeddedInApk", embedded);
        out.put("metadataApproved", metadataApproved);
        out.put("approvalReason", approvalReason(kind, metadata, metadataApproved));
        out.put("allowedForFutureExecution", basicAllowed && metadataApproved);
        if (basicAllowed && !metadataApproved) {
            out.put("approvalBlocker", "metadata de origem/licença pendente para asset externo");
        }
        out.put("placeholder", placeholder);
        return out;
    }

    private static JSONObject metadataFor(String kind, JSONObject localManifest, JSONObject sourcePlan) throws Exception {
        JSONObject out = new JSONObject();
        mergeMetadata(out, sourcePlan, kind);
        mergeMetadata(out, localManifest, kind);
        return out;
    }

    private static void mergeMetadata(JSONObject out, JSONObject payload, String kind) throws Exception {
        if (out == null || payload == null || kind == null) return;
        JSONObject targets = payload.optJSONObject("targets");
        if (targets == null) return;
        JSONObject target = targets.optJSONObject(kind);
        if (target == null) return;
        JSONObject metadata = target.optJSONObject("metadata");
        JSONObject source = metadata == null ? target : metadata;
        JSONArray names = source.names();
        if (names == null) return;
        for (int i = 0; i < names.length(); i++) {
            String name = names.optString(i, "");
            if (name == null || name.isEmpty() || "metadata".equals(name)) continue;
            Object value = source.opt(name);
            if (value != null) out.put(name, value);
        }
    }

    private static boolean metadataNeedsDependency(JSONObject info, String dependency) {
        if (info == null || dependency == null || dependency.trim().isEmpty()) return false;
        JSONObject metadata = info.optJSONObject("metadata");
        if (metadata == null) return false;
        JSONArray deps = metadata.optJSONArray("runtimeDependencies");
        if (deps != null) {
            for (int i = 0; i < deps.length(); i++) {
                if (dependency.equalsIgnoreCase(deps.optString(i, ""))) return true;
            }
        }
        String linkMode = lower(metadata.optString("linkMode", ""));
        if ("static".equals(linkMode) || "self-contained".equals(linkMode)) return false;
        String depPolicy = lower(metadata.optString("dependencyPolicy", ""));
        return depPolicy.contains(dependency.toLowerCase(Locale.ROOT));
    }

    private static boolean metadataApproved(String kind, JSONObject metadata) {
        String safeKind = kind == null ? "" : kind;
        if ("runner".equals(safeKind) || "executor".equals(safeKind)) return true;
        if (metadata == null) return false;
        String status = lower(metadata.optString("licenseStatus", ""));
        return "verified-audited".equals(status)
                || "source-built".equals(status)
                || "redistributable-verified".equals(status);
    }

    private static String approvalReason(String kind, JSONObject metadata, boolean ok) {
        String safeKind = kind == null ? "" : kind;
        if ("runner".equals(safeKind) || "executor".equals(safeKind)) return "asset interno do projeto";
        if (ok) return "metadata externa aprovada: " + (metadata == null ? "" : metadata.optString("licenseStatus", ""));
        return "metadata externa pendente: licenseStatus precisa ser verified-audited, source-built ou redistributable-verified";
    }

    private static JSONObject apkNativeInfo(Context context, String[] expectedNames) {
        JSONObject out = new JSONObject();
        try {
            out.put("present", false);
            out.put("path", "");
            out.put("name", "");
            out.put("size", 0L);
            out.put("canExecute", false);
            out.put("blockedByWritableAppHome", false);
            out.put("sha256", "");
            out.put("detectedBy", "missing");
            out.put("sourceApk", "");
            out.put("zipEntry", "");
            out.put("compressionMethod", "");
            ApplicationInfo app = context == null ? null : context.getApplicationInfo();
            if (app == null || expectedNames == null) return out;
            String[] apkPaths = mergeApkPaths(app.sourceDir, app.splitSourceDirs);
            for (String apkPath : apkPaths) {
                if (apkPath == null || apkPath.trim().isEmpty()) continue;
                File apk = new File(apkPath);
                if (!apk.exists() || !apk.isFile()) continue;
                ZipFile zip = null;
                try {
                    zip = new ZipFile(apk);
                    for (String expectedName : expectedNames) {
                        if (expectedName == null || expectedName.trim().isEmpty()) continue;
                        String entryName = "lib/arm64-v8a/" + expectedName;
                        ZipEntry entry = zip.getEntry(entryName);
                        if (entry == null || entry.isDirectory()) continue;
                        long size = Math.max(0L, entry.getSize());
                        String digest = "";
                        if (size > 0L && size <= 64L * 1024L * 1024L) {
                            InputStream in = zip.getInputStream(entry);
                            digest = sha256(in);
                        }
                        out.put("present", size > 0L);
                        out.put("path", path(apk) + "!/" + entryName);
                        out.put("name", expectedName);
                        out.put("size", size);
                        out.put("canExecute", size > 0L);
                        out.put("blockedByWritableAppHome", false);
                        out.put("sha256", digest);
                        out.put("detectedBy", "apk-native-lib-entry");
                        out.put("sourceApk", path(apk));
                        out.put("zipEntry", entryName);
                        out.put("compressionMethod", entry.getMethod() == ZipEntry.STORED ? "stored" : "deflated");
                        return out;
                    }
                } catch (Throwable ignored) {
                } finally {
                    try { if (zip != null) zip.close(); } catch (Throwable ignored) {}
                }
            }
        } catch (Throwable ignored) {}
        return out;
    }

    private static String[] mergeApkPaths(String sourceDir, String[] splitSourceDirs) {
        int splitCount = splitSourceDirs == null ? 0 : splitSourceDirs.length;
        String[] out = new String[1 + splitCount];
        out[0] = sourceDir == null ? "" : sourceDir;
        for (int i = 0; i < splitCount; i++) {
            out[i + 1] = splitSourceDirs[i];
        }
        return out;
    }

    private static JSONObject fileInfo(File file, File dataDir) throws Exception {
        JSONObject out = new JSONObject();
        boolean present = file != null && file.exists();
        out.put("present", present);
        out.put("path", file == null ? "" : path(file));
        out.put("name", file == null ? "" : file.getName());
        out.put("size", present ? file.length() : 0L);
        out.put("canExecute", present && file.canExecute());
        out.put("blockedByWritableAppHome", present && dataDir != null && Build.VERSION.SDK_INT >= 29 && isInside(file, dataDir));
        out.put("sha256", present && file.isFile() && file.length() <= 64L * 1024L * 1024L ? sha256(file) : "");
        return out;
    }

    private static File firstExisting(File... files) {
        if (files == null) return null;
        for (File file : files) {
            if (file != null && file.exists()) return file;
        }
        return null;
    }

    private static File firstExisting(File dir, String... names) {
        if (dir == null || names == null) return null;
        for (String name : names) {
            File file = new File(dir, name);
            if (file.exists()) return file;
        }
        return null;
    }

    private static String sha256(File file) {
        try {
            return sha256(new FileInputStream(file));
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static String sha256(InputStream in) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] buffer = new byte[64 * 1024];
            int read;
            while ((read = in.read(buffer)) > 0) {
                digest.update(buffer, 0, read);
            }
            in.close();
            byte[] bytes = digest.digest();
            StringBuilder hex = new StringBuilder(bytes.length * 2);
            for (byte b : bytes) {
                hex.append(String.format(Locale.ROOT, "%02x", b & 0xff));
            }
            return hex.toString();
        } catch (Throwable ignored) {
            try { if (in != null) in.close(); } catch (Throwable ignored2) {}
            return "";
        }
    }

    private static boolean isInside(File child, File parent) {
        try {
            String childPath = child.getCanonicalPath();
            String parentPath = parent.getCanonicalPath();
            return childPath.equals(parentPath) || childPath.startsWith(parentPath + File.separator);
        } catch (Throwable ignored) {
            return false;
        }
    }

    private static void ensureBase(Layout layout) {
        layout.core.mkdirs();
        layout.runtime.mkdirs();
        layout.logs.mkdirs();
        layout.rootfs.mkdirs();
        new File(layout.core, "bedrock/runtime").mkdirs();
        new File(layout.core, "bedrock/logs").mkdirs();
    }

    private static JSONObject readAssetJson(Context context, String name) {
        try {
            if (context == null || name == null || name.trim().isEmpty()) return new JSONObject();
            InputStream in = context.getAssets().open(name);
            StringBuilder builder = new StringBuilder();
            byte[] buffer = new byte[4096];
            int read;
            while ((read = in.read(buffer)) > 0 && builder.length() < TEXT_LIMIT) {
                builder.append(new String(buffer, 0, read, StandardCharsets.UTF_8));
            }
            in.close();
            if (builder.length() == 0) return new JSONObject();
            return new JSONObject(builder.toString());
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private static JSONObject readJson(File file) {
        try {
            if (file == null || !file.exists()) return new JSONObject();
            BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(file), StandardCharsets.UTF_8));
            StringBuilder builder = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null && builder.length() < TEXT_LIMIT) {
                builder.append(line).append('\n');
            }
            reader.close();
            if (builder.length() == 0) return new JSONObject();
            return new JSONObject(builder.toString());
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private static void writeJson(File file, JSONObject json) throws Exception {
        File parent = file.getParentFile();
        if (parent != null) parent.mkdirs();
        FileOutputStream out = new FileOutputStream(file, false);
        out.write(json.toString(2).getBytes(StandardCharsets.UTF_8));
        out.flush();
        out.close();
    }

    private static void appendLog(File file, String message) {
        try {
            File parent = file.getParentFile();
            if (parent != null) parent.mkdirs();
            FileOutputStream out = new FileOutputStream(file, true);
            String line = System.currentTimeMillis() + " " + (message == null ? "" : clean(message, 500)) + "\n";
            out.write(line.getBytes(StandardCharsets.UTF_8));
            out.close();
        } catch (Throwable ignored) {}
    }

    private static File resolveCoreLinuxDir(Context context, File provided) {
        if (provided != null) return provided;
        return new File(context.getFilesDir(), "core-linux");
    }

    private static String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String value : values) {
            if (value != null && !value.trim().isEmpty()) return value.trim();
        }
        return "";
    }

    private static String lower(String value) {
        return value == null ? "" : value.toLowerCase(Locale.ROOT);
    }

    private static String clean(String value, int limit) {
        String text = value == null ? "" : value.replace('\n', ' ').replace('\r', ' ').trim();
        if (limit > 0 && text.length() > limit) return text.substring(0, limit - 1) + "…";
        return text;
    }

    private static String shortThrowable(Throwable exc) {
        if (exc == null) return "erro desconhecido";
        String msg = exc.getClass().getSimpleName() + (exc.getMessage() == null ? "" : ": " + exc.getMessage());
        return clean(msg, 300);
    }

    private static String path(File file) {
        return file == null ? "" : file.getAbsolutePath();
    }

    private static final class Layout {
        final File core;
        final File runtime;
        final File logs;
        final File rootfs;

        Layout(File core) {
            this.core = core;
            this.runtime = new File(core, "runtime");
            this.logs = new File(core, "logs");
            this.rootfs = new File(core, "rootfs");
        }
    }
}
