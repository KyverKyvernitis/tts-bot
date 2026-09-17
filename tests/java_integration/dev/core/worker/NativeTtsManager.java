package dev.core.worker; import org.json.JSONObject; final class NativeTtsManager {
 JSONObject last;int syntheses,rawCalls,voiceCalls; NativeTtsManager(){} NativeTtsManager(android.content.Context c){} NativeTtsManager(android.content.Context c,android.content.SharedPreferences p){}
 JSONObject statusJson(){return new JSONObject().put("ok",true).put("ready",true);}
 JSONObject synthesize(JSONObject body){last=body;syntheses++;return new JSONObject().put("ok",true).put("audio_format","wav").put("data_b64","YXVkaW8=");}
 JSONObject voicesJson(JSONObject body){last=body;voiceCalls++;return statusJson();}
 SynthesisResult synthesizeRaw(JSONObject body){last=body;rawCalls++;return new SynthesisResult();}
 void shutdown(){} void warmUp(){}
 static class SynthesisResult { byte[] data=new byte[0];String audioFormat="wav",sha256="",voice="",localeTag="";long synthMs=1; }
}
