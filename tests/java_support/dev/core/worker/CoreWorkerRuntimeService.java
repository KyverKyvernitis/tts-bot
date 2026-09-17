package dev.core.worker;
import android.content.Context;
final class CoreWorkerRuntimeService {
    static int starts, polls;
    static void requestStart(Context context, String reason) { starts++; }
    static void requestPoll(Context context, String reason) { polls++; }
}
