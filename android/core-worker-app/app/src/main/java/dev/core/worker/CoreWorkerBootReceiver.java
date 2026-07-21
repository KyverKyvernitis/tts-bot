package dev.core.worker;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

public class CoreWorkerBootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        try {
            String action = intent == null ? "receiver" : String.valueOf(intent.getAction());
            try {
                SharedPreferences prefs = context.getSharedPreferences("core_worker_private", Context.MODE_PRIVATE);
                prefs.edit()
                        .putString("internal_jobs_wake_reason", action)
                        .putLong("internal_jobs_wake_requested_at", System.currentTimeMillis())
                        .putString("native_boot_state", "receiver acionado")
                        .apply();
            } catch (Throwable ignored) {
            }
            CoreWorkerUpdateJobService.schedule(context, action);
            try {
                SharedPreferences prefs = context.getSharedPreferences("core_worker_private", Context.MODE_PRIVATE);
                boolean shouldRun = CoreWorkerRuntimeService.shouldRunAgent(context);
                if (shouldRun) {
                    // Persiste a migração de instalações antigas que ainda não tinham agent_enabled.
                    if (!prefs.contains("agent_enabled")) {
                        prefs.edit().putBoolean("agent_enabled", true).apply();
                    }
                    CoreWorkerRuntimeService.requestStart(context, action == null ? "boot" : action);
                }
            } catch (Throwable ignored) {
            }
        } catch (Throwable ignored) {
        }
    }
}
