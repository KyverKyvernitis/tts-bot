package dev.core.worker;
import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONObject;
final class CoreWorkerDirectTaskExecutor {
    CoreWorkerDirectTaskExecutor(Context c, SharedPreferences p, NativeTtsManager t) { }
    static boolean supports(String task) { return "ping".equals(task); }
    JSONObject execute(JSONObject body) { return health(); }
    JSONObject health() { return new JSONObject().put("ok", true); }
    NativeTtsManager.SynthesisResult synthesizeRaw(JSONObject body) { return new NativeTtsManager.SynthesisResult(); }
}
