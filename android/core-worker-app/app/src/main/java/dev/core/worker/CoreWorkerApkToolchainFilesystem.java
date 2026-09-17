package dev.core.worker;

import android.content.SharedPreferences;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.LinkOption;

/** Durable fingerprints travel with move-only toolchain directories. */
final class CoreWorkerApkToolchainFilesystem {
    // Preserve the last confirmed identity across commit(false), which also
    // changes SharedPreferences' in-memory map. A retry must run smoke again.
    private static final java.util.WeakHashMap<SharedPreferences, String> dirty = new java.util.WeakHashMap<>();
    private static final String CURRENT = "apk_self_builder_toolchain_fingerprint";
    private static final String PREVIOUS = "apk_self_builder_previous_toolchain_fingerprint";
    private static final String PENDING = "apk_self_builder_pending_toolchain_fingerprint";
    private static final String GOOD = "apk_self_builder_known_good_toolchain_fingerprint";
    private static final String MARKER = ".release-fingerprint";
    private CoreWorkerApkToolchainFilesystem() { }

    private static void commit(SharedPreferences prefs, SharedPreferences.Editor editor) {
        if (!dirty.containsKey(prefs)) dirty.put(prefs, prefs.getString(GOOD, ""));
        CoreWorkerRuntimeIdentity.requireCommit(editor);
        dirty.remove(prefs);
    }

    static String fingerprint(File directory) throws IOException {
        requireMetadata(directory);
        String value = new String(CoreWorkerAtomicTextFiles.read(new File(directory, MARKER), 128), StandardCharsets.UTF_8).trim();
        if (!value.matches("[0-9a-f]{64}") && !"legacy-assets".equals(value)) throw new IOException("fingerprint de toolchain inválido");
        return value;
    }

    private static void mark(File directory, String value) throws IOException {
        if (!value.matches("[0-9a-f]{64}") && !"legacy-assets".equals(value)) throw new IOException("fingerprint de toolchain inválido");
        CoreLinuxRootfsFilesystem.requireDirectory(directory);
        requireMetadata(directory);
        CoreWorkerAtomicTextFiles.write(new File(directory, MARKER), value + "\n");
    }

    private static void requireMetadata(File directory) throws IOException {
        CoreLinuxRootfsFilesystem.requireDirectory(directory);
        if (!Files.isRegularFile(new File(directory, "manifest.json").toPath(), LinkOption.NOFOLLOW_LINKS)) {
            throw new IOException("toolchain sem manifesto regular");
        }
        for (String suffix : new String[]{"", ".bak", ".new"}) {
            File marker = new File(directory, MARKER + suffix);
            if (CoreLinuxRootfsFilesystem.exists(marker)
                    && !Files.isRegularFile(marker.toPath(), LinkOption.NOFOLLOW_LINKS)) {
                throw new IOException("marcador de toolchain inválido");
            }
        }
    }

    static void recover(File builder, SharedPreferences prefs) throws IOException {
        synchronized (CoreLinuxRootfsFilesystem.LOCK) {
            File active = new File(builder, "toolchain"), previous = new File(builder, "toolchain-previous");
            boolean restored = CoreLinuxRootfsFilesystem.recover(active, previous);
            if (!CoreLinuxRootfsFilesystem.exists(active)) return;
            requireMetadata(active);
            if (!CoreLinuxRootfsFilesystem.exists(new File(active, MARKER))
                    && !CoreLinuxRootfsFilesystem.exists(new File(active, MARKER + ".bak"))) {
                // One-time migration of a pre-modular installation, before any moves.
                String old = prefs.getString(CURRENT, "");
                mark(active, old.matches("[0-9a-f]{64}") ? old : "legacy-assets");
            }
            String current = fingerprint(active);
            if (CoreLinuxRootfsFilesystem.exists(previous)) requireMetadata(previous);
            if (CoreLinuxRootfsFilesystem.exists(previous) && !CoreLinuxRootfsFilesystem.exists(new File(previous, MARKER))
                    && !CoreLinuxRootfsFilesystem.exists(new File(previous, MARKER + ".bak"))) {
                String oldMarker = prefs.getString(PREVIOUS, "");
                mark(previous, oldMarker.matches("[0-9a-f]{64}") ? oldMarker : "legacy-assets");
            }
            String old = CoreLinuxRootfsFilesystem.exists(previous) ? fingerprint(previous) : "";
            String good = dirty.containsKey(prefs) ? dirty.get(prefs) : prefs.getString(GOOD, "");
            String pending = current.equals(good) ? "" : current;
            if (dirty.containsKey(prefs) || restored || !current.equals(prefs.getString(CURRENT, ""))
                    || !pending.equals(prefs.getString(PENDING, ""))
                    || !old.equals(prefs.getString(PREVIOUS, ""))) {
                SharedPreferences.Editor editor = prefs.edit().putString(CURRENT, current).putString(PREVIOUS, old)
                        .putString(GOOD, good);
                if (pending.isEmpty()) editor.remove(PENDING); else editor.putString(PENDING, pending);
                if (restored) editor.putString("apk_self_builder_toolchain_update_state", "recovered");
                else if (!pending.isEmpty()) editor.putString("apk_self_builder_toolchain_update_state", "verifying_runtime");
                commit(prefs, editor);
            }
        }
    }

    static void promote(File builder, File active, File next, SharedPreferences prefs, String newFingerprint) throws Exception {
        synchronized (CoreLinuxRootfsFilesystem.LOCK) {
            if (!active.getCanonicalFile().equals(new File(builder, "toolchain").getCanonicalFile())) {
                throw new IOException("toolchain ativa fora do builder");
            }
            CoreLinuxRootfsFilesystem.requireDisjoint(active, next, new File(builder, "toolchain-previous"));
            requireMetadata(next);
            recover(builder, prefs);
            mark(next, newFingerprint);
            String old = CoreLinuxRootfsFilesystem.exists(active) ? fingerprint(active) : "";
            // Intent is durable before current -> previous. Recovery reconciles it with directory markers.
            commit(prefs, prefs.edit().putString(PREVIOUS, old)
                    .putString(PENDING, newFingerprint).putString("apk_self_builder_toolchain_update_state", "promoting"));
            CoreLinuxRootfsFilesystem.promote(active, next, new File(builder, "toolchain-previous"));
            commit(prefs, prefs.edit().putString(CURRENT, newFingerprint)
                    .putString(PENDING, newFingerprint).putString(PREVIOUS, old)
                    .putLong("apk_self_builder_toolchain_promoted_at", System.currentTimeMillis()));
        }
    }

    static String confirm(File builder, SharedPreferences prefs) throws Exception {
        synchronized (CoreLinuxRootfsFilesystem.LOCK) {
            recover(builder, prefs);
            String current = fingerprint(new File(builder, "toolchain"));
            commit(prefs, prefs.edit().putString(CURRENT, current).putString(GOOD, current)
                    .remove(PENDING).putString("apk_self_builder_toolchain_update_state", "succeeded")
                    .putString("apk_self_builder_toolchain_update_error", "")
                    .putLong("apk_self_builder_toolchain_verified_at", System.currentTimeMillis()));
            return current;
        }
    }

    static boolean reuse(File builder, SharedPreferences prefs, String expected) throws IOException {
        synchronized (CoreLinuxRootfsFilesystem.LOCK) {
            recover(builder, prefs);
            File active = new File(builder, "toolchain");
            if (!CoreLinuxRootfsFilesystem.exists(active) || !fingerprint(active).equals(expected)) return false;
            boolean verified = expected.equals(prefs.getString(GOOD, "")) && prefs.getString(PENDING, "").isEmpty();
            commit(prefs, prefs.edit().putString("apk_self_builder_toolchain_update_state",
                    verified ? "succeeded" : "verifying_runtime"));
            return true;
        }
    }

    static String rollback(File builder, SharedPreferences prefs, String detail) throws Exception {
        synchronized (CoreLinuxRootfsFilesystem.LOCK) {
            File active = new File(builder, "toolchain"), previous = new File(builder, "toolchain-previous");
            File failed = new File(builder, "toolchain-failed");
            CoreLinuxRootfsFilesystem.requireDirectory(previous);
            String previousFingerprint = fingerprint(previous);
            CoreLinuxRootfsFilesystem.removeTree(failed);
            if (CoreLinuxRootfsFilesystem.exists(active)) CoreLinuxRootfsFilesystem.move(active, failed);
            try { CoreLinuxRootfsFilesystem.move(previous, active); }
            catch (IOException error) {
                try { recover(builder, prefs); } catch (Exception recovery) { error.addSuppressed(recovery); }
                throw error;
            }
            String good = dirty.containsKey(prefs) ? dirty.get(prefs) : prefs.getString(GOOD, "");
            SharedPreferences.Editor editor = prefs.edit().putString(CURRENT, previousFingerprint)
                    .putString(PREVIOUS, "").putString(GOOD, good)
                    .putString("apk_self_builder_toolchain_update_state", "rolled_back")
                    .putString("apk_self_builder_toolchain_update_error", detail == null ? "" : detail)
                    .putLong("apk_self_builder_toolchain_verified_at", System.currentTimeMillis());
            if (previousFingerprint.equals(good)) editor.remove(PENDING);
            else editor.putString(PENDING, previousFingerprint);
            commit(prefs, editor);
            return previousFingerprint;
        }
    }
}
