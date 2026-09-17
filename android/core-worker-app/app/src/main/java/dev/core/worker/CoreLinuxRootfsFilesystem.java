package dev.core.worker;

import java.io.File;
import java.io.IOException;
import java.nio.file.*;
import java.nio.file.attribute.BasicFileAttributes;

/** Move-only directory promotion and cleanup which never traverses symbolic links. */
final class CoreLinuxRootfsFilesystem {
    static final Object LOCK = new Object();
    private CoreLinuxRootfsFilesystem() { }

    static boolean exists(File file) { return Files.exists(file.toPath(), LinkOption.NOFOLLOW_LINKS); }

    static void requireDirectory(File file) throws IOException {
        if (!Files.isDirectory(file.toPath(), LinkOption.NOFOLLOW_LINKS)) throw new IOException("diretório inválido: " + file.getName());
    }

    static void move(File from, File to) throws IOException {
        File parent = to.getAbsoluteFile().getParentFile();
        Files.createDirectories(parent.toPath());
        // No copy fallback: an interrupted copy cannot be an active installation.
        Files.move(from.toPath(), to.toPath());
    }

    static void replaceFile(File staged, File target) throws IOException {
        if (!Files.isRegularFile(staged.toPath(), LinkOption.NOFOLLOW_LINKS)
                || (exists(target) && !Files.isRegularFile(target.toPath(), LinkOption.NOFOLLOW_LINKS))) {
            throw new IOException("arquivo de promoção inválido");
        }
        // Same-filesystem atomic replacement preserves the old destination if
        // promotion fails. Unsupported atomic moves fail without a copy fallback.
        Files.move(staged.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    static boolean recover(File active, File previous) throws IOException {
        synchronized (LOCK) {
            requireDisjoint(active, previous);
            if (!exists(active) && exists(previous)) {
                requireDirectory(previous);
                move(previous, active);
                return true;
            }
            if (exists(active)) requireDirectory(active);
            return false;
        }
    }

    static void promote(File active, File next, File previous) throws IOException {
        synchronized (LOCK) {
            requireDisjoint(active, next, previous);
            requireDirectory(next);
            recover(active, previous);
            removeTree(previous);
            if (exists(active)) move(active, previous);
            try { move(next, active); }
            catch (IOException failure) {
                try { recover(active, previous); }
                catch (IOException rollback) { failure.addSuppressed(rollback); }
                throw failure;
            }
        }
    }

    static void requireDisjoint(File... directories) throws IOException {
        for (int i = 0; i < directories.length; i++) {
            Path first = directories[i].getCanonicalFile().toPath();
            for (int j = i + 1; j < directories.length; j++) {
                Path second = directories[j].getCanonicalFile().toPath();
                if (first.startsWith(second) || second.startsWith(first)) {
                    throw new IOException("diretórios transacionais sobrepostos");
                }
            }
        }
    }

    static long removeTree(File file) throws IOException {
        if (file == null || !exists(file)) return 0L;
        final long[] removed = {0L};
        // walkFileTree without FOLLOW_LINKS visits a symlink as one leaf.
        Files.walkFileTree(file.toPath(), new SimpleFileVisitor<Path>() {
            @Override public FileVisitResult visitFile(Path path, BasicFileAttributes attrs) throws IOException {
                long size = attrs.isRegularFile() ? attrs.size() : 0L;
                Files.delete(path);
                removed[0] += size;
                return FileVisitResult.CONTINUE;
            }
            @Override public FileVisitResult postVisitDirectory(Path path, IOException failure) throws IOException {
                if (failure != null) throw failure;
                Files.delete(path);
                return FileVisitResult.CONTINUE;
            }
        });
        return removed[0];
    }
}
