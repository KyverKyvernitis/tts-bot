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
                if (prefs.getBoolean("foreground_runtime_active", false)) {
                    Intent service = new Intent(context, CoreWorkerRuntimeService.class);
                    service.setAction(CoreWorkerRuntimeService.ACTION_START);
                    service.putExtra("reason", action == null ? "boot" : action);
                    if (android.os.Build.VERSION.SDK_INT >= 26) {
                        context.startForegroundService(service);
                    } else {
                        context.startService(service);
                    }
                }
            } catch (Throwable ignored) {
            }
        } catch (Throwable ignored) {
        }
    }
}
