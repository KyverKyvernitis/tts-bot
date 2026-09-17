package dev.core.worker;

import android.util.AtomicFile;
import java.io.File;
import java.nio.file.Files;
import java.nio.file.Path;
import static dev.core.worker.WorkerHarness.*;
import static dev.core.worker.FilesystemHarness.*;

final class RecoveryHarness {
    static final String PREFIX = "apk_self_builder_";
    static FakePreferences reopen(FakePreferences prefs) {
        FakePreferences fresh = new FakePreferences();
        fresh.memory.putAll(prefs.disk);
        fresh.disk.putAll(prefs.disk);
        return fresh;
    }

    static void run(String test) throws Exception {
        Path root = Files.createTempDirectory("worker-recovery-");
        try {
            File active = root.resolve("toolchain").toFile();
            File previous = root.resolve("toolchain-previous").toFile();
            File next = root.resolve("toolchain-next").toFile();
            if (test.equals("overlapping_paths")) {
                toolchain(active, OLD);
                toolchain(next, NEW);
                fails(() -> CoreLinuxRootfsFilesystem.promote(active, next, active));
                check(fingerprint(active).equals(OLD), "equal previous destroyed active");
                File nested = new File(active, "nested");
                toolchain(nested, NEW);
                fails(() -> CoreLinuxRootfsFilesystem.promote(active, nested, previous));
                check(fingerprint(active).equals(OLD), "nested staging moved active");
                fails(() -> CoreLinuxRootfsFilesystem.recover(active, nested));
                return;
            }
            if (test.equals("binary_replacement")) {
                Path file = root.resolve("asset"), staged = root.resolve("asset.tmp");
                Files.writeString(file, "old");
                Files.createDirectory(staged);
                fails(() -> CoreLinuxRootfsFilesystem.replaceFile(staged.toFile(), file.toFile()));
                check(Files.readString(file).equals("old"), "failed replacement deleted target");
                Files.delete(staged); Files.writeString(staged, "new");
                CoreLinuxRootfsFilesystem.replaceFile(staged.toFile(), file.toFile());
                check(Files.readString(file).equals("new") && !Files.exists(staged), "replacement failed");
                return;
            }
            if (test.equals("atomic_silent_finish")) {
                File state = root.resolve("state").toFile();
                CoreWorkerAtomicTextFiles.write(state, "old");
                AtomicFile.ignoreNextFinish = true;
                fails(() -> CoreWorkerAtomicTextFiles.write(state, "new"));
                check(Files.readString(state.toPath()).equals("old"), "failed finish lost old text");
                return;
            }
            FakePreferences prefs = new FakePreferences();
            toolchain(active, OLD);
            CoreWorkerApkToolchainFilesystem.recover(root.toFile(), prefs);
            CoreWorkerApkToolchainFilesystem.confirm(root.toFile(), prefs);
            if (test.startsWith("metadata_symlink_")) {
                String name = test.endsWith("manifest") ? "manifest.json" : ".release-fingerprint";
                Path outside = root.resolve("outside");
                Files.writeString(outside, name.equals("manifest.json") ? "{}" : OLD);
                Path link = active.toPath().resolve(name);
                Files.delete(link);
                Files.createSymbolicLink(link, outside);
                FakePreferences currentPrefs = prefs;
                fails(() -> CoreWorkerApkToolchainFilesystem.recover(root.toFile(), currentPrefs));
                check(Files.readString(outside).equals(name.equals("manifest.json") ? "{}" : OLD), "metadata altered outside");
                return;
            }
            toolchain(next, NEW);
            if (test.equals("missing_manifest")) {
                Files.delete(next.toPath().resolve("manifest.json"));
                FakePreferences currentPrefs = prefs;
                fails(() -> CoreWorkerApkToolchainFilesystem.promote(root.toFile(), active, next, currentPrefs, NEW));
                check(fingerprint(active).equals(OLD), "invalid staging became active");
                return;
            }
            CoreWorkerApkToolchainFilesystem.promote(root.toFile(), active, next, prefs, NEW);
            if (test.equals("reuse_pending")) {
                check(CoreWorkerApkToolchainFilesystem.reuse(root.toFile(), prefs, NEW), "same candidate not reused");
                check("verifying_runtime".equals(prefs.disk.get(PREFIX + "toolchain_update_state")), "reuse declared early success");
                check(OLD.equals(prefs.disk.get(PREFIX + "known_good_toolchain_fingerprint")), "reuse bypassed smoke");
                CoreWorkerApkToolchainFilesystem.confirm(root.toFile(), prefs);
                CoreWorkerApkToolchainFilesystem.reuse(root.toFile(), prefs, NEW);
                check("succeeded".equals(prefs.disk.get(PREFIX + "toolchain_update_state")), "verified reuse lost success");
                return;
            }
            if (test.startsWith("confirm_failure")) {
                prefs.failNext = 1;
                FakePreferences currentPrefs = prefs;
                fails(() -> CoreWorkerApkToolchainFilesystem.confirm(root.toFile(), currentPrefs));
                if (test.endsWith("reopen")) prefs = reopen(prefs);
                CoreWorkerApkToolchainFilesystem.recover(root.toFile(), prefs);
                check(OLD.equals(prefs.disk.get(PREFIX + "known_good_toolchain_fingerprint")), "failed confirm became good");
                check(NEW.equals(prefs.disk.get(PREFIX + "pending_toolchain_fingerprint")), "failed confirm lost pending");
                check(!"succeeded".equals(prefs.getString(PREFIX + "toolchain_update_state", "")), "failed confirm advertised success");
                CoreWorkerApkToolchainFilesystem.confirm(root.toFile(), prefs);
                check(NEW.equals(prefs.disk.get(PREFIX + "known_good_toolchain_fingerprint")), "retry did not confirm");
                return;
            }
            if (test.equals("rollback_commit_failure")) {
                prefs.failNext = 1;
                FakePreferences currentPrefs = prefs;
                fails(() -> CoreWorkerApkToolchainFilesystem.rollback(root.toFile(), currentPrefs, "bad smoke"));
            } else {
                CoreLinuxRootfsFilesystem.move(active, root.resolve("toolchain-failed").toFile());
                if (test.equals("rollback_after_restore")) CoreLinuxRootfsFilesystem.move(previous, active);
                else check(test.equals("rollback_after_failed"), "unknown recovery test");
                prefs = reopen(prefs);
            }
            CoreWorkerApkToolchainFilesystem.recover(root.toFile(), prefs);
            check(fingerprint(active).equals(OLD), "recovery lost previous content");
            check(fingerprint(root.resolve("toolchain-failed").toFile()).equals(NEW), "recovery lost failed evidence");
            check(OLD.equals(prefs.disk.get(PREFIX + "toolchain_fingerprint")), "disk disagrees with active");
            check(!prefs.disk.containsKey(PREFIX + "pending_toolchain_fingerprint"), "rollback still pending");
        } finally {
            CoreLinuxRootfsFilesystem.removeTree(root.toFile());
        }
    }

    static String fingerprint(File dir) throws Exception {
        return CoreWorkerApkToolchainFilesystem.fingerprint(dir);
    }
}
