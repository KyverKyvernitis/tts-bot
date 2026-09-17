package dev.core.worker;
import android.content.Context;
final class CoreWorkerApkBuildManager {
    static int refreshes;
    static void refreshAsync(Context context) { refreshes++; }
}
