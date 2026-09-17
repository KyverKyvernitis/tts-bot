package dev.core.worker;
import org.json.JSONObject;
final class NativeTtsManager {
    JSONObject statusJson() { return new JSONObject().put("ok", true); }
    JSONObject synthesize(JSONObject body) { return statusJson(); }
    JSONObject voicesJson(JSONObject body) { return statusJson(); }
    SynthesisResult synthesizeRaw(JSONObject body) { return new SynthesisResult(); }
    static final class SynthesisResult {
        byte[] data = new byte[0];
        String audioFormat = "wav", sha256 = "", voice = "", localeTag = "";
        long synthMs = 1;
    }
}
