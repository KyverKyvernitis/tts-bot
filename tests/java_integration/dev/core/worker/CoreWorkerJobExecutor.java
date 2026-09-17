package dev.core.worker; final class CoreWorkerJobExecutor {
 CoreWorkerJobExecutor(android.content.Context context){} org.json.JSONObject execute(org.json.JSONObject job,String serverUrl)throws Exception{return new org.json.JSONObject().put("ok",true);}
 java.io.File outboxDir(){return null;}
}
