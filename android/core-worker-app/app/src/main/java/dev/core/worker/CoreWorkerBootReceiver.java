package dev.core.worker;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public class CoreWorkerBootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        try {
            String action = intent == null ? "receiver" : String.valueOf(intent.getAction());
            CoreWorkerUpdateJobService.schedule(context, action);
        } catch (Throwable ignored) {
        }
    }
}
