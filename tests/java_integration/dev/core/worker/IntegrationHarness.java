package dev.core.worker;

import android.content.Context;
import android.content.Intent;
import android.app.job.JobParameters;
import com.chaquo.python.Python;
import com.chaquo.python.PyObject;
import com.sun.net.httpserver.HttpServer;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.File;
import java.lang.reflect.*;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public final class IntegrationHarness {
    interface Checked { void run() throws Exception; }
    static void check(boolean value,String message){if(!value)throw new AssertionError(message);}
    static void await(CountDownLatch latch)throws Exception{check(latch.await(5,TimeUnit.SECONDS),"latch timed out");}
    static void waitFor(java.util.function.BooleanSupplier condition)throws Exception{
        long until=System.nanoTime()+5_000_000_000L;
        while(!condition.getAsBoolean()&&System.nanoTime()<until)Thread.sleep(10);
        check(condition.getAsBoolean(),"condition timed out");
    }
    static void rejects(Checked task,String text)throws Exception{
        try{task.run();}catch(Exception expected){check(expected.getMessage()!=null&&expected.getMessage().contains(text),"wrong rejection: "+expected);return;}
        throw new AssertionError("invalid input accepted: "+text);
    }
    static Object invoke(Object target,String name,Class<?>[] types,Object...args)throws Exception{
        Method method=target.getClass().getDeclaredMethod(name,types);method.setAccessible(true);
        try{return method.invoke(target,args);}catch(InvocationTargetException wrapped){Throwable error=wrapped.getCause();if(error instanceof Exception)throw (Exception)error;throw new AssertionError(error);}
    }
    static Object field(Object target,String name)throws Exception{
        Class<?> type=target instanceof Class?(Class<?>)target:target.getClass();
        Field field=type.getDeclaredField(name);field.setAccessible(true);return field.get(target instanceof Class?null:target);
    }
    static void set(Object target,String name,Object value)throws Exception{
        Class<?> type=target instanceof Class?(Class<?>)target:target.getClass();
        Field field=type.getDeclaredField(name);field.setAccessible(true);field.set(target instanceof Class?null:target,value);
    }
    static void cached()throws Exception{
        set(CoreWorkerApkBuildManager.class,"cachedPreflight",new JSONObject().put("ready",false).put("ok",false));
        set(CoreWorkerApkBuildManager.class,"cachedPreflightAt",System.currentTimeMillis());
    }
    static void pair(FakePreferences prefs,String url){
        CoreWorkerRuntimeIdentity.markDedicatedApkPair(prefs,prefs.edit().putString("worker_id","apk-test")
                .putString("worker_token","test-token").putString("server_url",url).putBoolean("agent_enabled",true),"apk-test");
    }
    public static void main(String[] args)throws Exception{
        Path root=Files.createTempDirectory("worker-integration-");
        try{
            FakePreferences prefs=new FakePreferences();Context context=new Context(prefs,root.toFile());cached();
            String scenario=args[0];
            if(scenario.startsWith("direct_")){direct(scenario,context,prefs);return;}
            if(scenario.equals("builder_pin")){builder(context,prefs);return;}
            if(scenario.equals("update_generations")){updates(context,prefs);return;}
            service(scenario,context,prefs);
        }finally{CoreLinuxRootfsFilesystem.removeTree(root.toFile());}
    }
    static void direct(String scenario,Context context,FakePreferences prefs)throws Exception{
        NativeTtsManager tts=new NativeTtsManager();
        CoreWorkerDirectTaskExecutor dispatcher=new CoreWorkerDirectTaskExecutor(context,prefs,tts);
        if(scenario.equals("direct_tts")){
            for(String task:new String[]{"tts_agent_synthesize","tts_synthesize_benchmark","tts_synthesize_piper"}){
                JSONObject body=new JSONObject().put("task",task).put("text","Olá").put("engine","android_native")
                        .put("voice","voice-test").put("locale","pt-BR").put("rate","+15%").put("pitch","-3Hz");
                String before=body.toString();JSONObject result=dispatcher.execute(body);
                check(tts.last==body&&before.equals(body.toString()),"TTS changed parameters or reference");
                check(result.getString("data_b64").equals("YXVkaW8=")&&result.getString("audio_format").equals("wav"),"TTS audio contract changed");
            }
            check(tts.syntheses==3,"aliases used another TTS owner");
            for(String task:new String[]{"tts_android_voices","tts_atts_voices","android_tts_voices"}){
                JSONObject body=new JSONObject().put("task",task).put("locale","pt-BR");dispatcher.execute(body);check(tts.last==body,"voices copied input");
            }
            JSONObject raw=new JSONObject().put("text","raw");dispatcher.synthesizeRaw(raw);
            check(tts.last==raw&&tts.rawCalls==1&&tts.voiceCalls==3,"raw/voice routes used another TTS owner");
            JSONObject store=new JSONObject().put("task","tts_cache_store").put("key","same-key").put("data_b64","YXVkaW8=");
            dispatcher.execute(store);JSONObject hit=dispatcher.execute(new JSONObject().put("task","tts_cache_lookup").put("key","same-key"));
            check(hit.getBoolean("hit")&&hit.getString("data_b64").equals("YXVkaW8=")&&tts.syntheses==3,"cache hit invoked synthesis or changed bytes");
            return;
        }
        if(scenario.equals("direct_invalid")){
            JSONObject unknown=dispatcher.execute(new JSONObject().put("task","shell").put("command","echo unsafe"));
            check(!unknown.getBoolean("ok")&&unknown.getString("runtime_mode").equals("apk-native-direct"),"unknown task not structured");
            rejects(()->dispatcher.execute(new JSONObject().put("task","zip").put("files",new JSONArray().put(new JSONObject().put("name","../outside").put("text","bad")))),"ZIP");
            rejects(()->dispatcher.execute(new JSONObject().put("task","sha256").put("data_b64","%%%")),"base64");
            check(!Files.exists(context.root.toPath().resolve("outside")),"ZIP escaped destination");
            Path binary=context.root.toPath().resolve("native/libcoreworker_ffmpeg.so");
            Files.createDirectories(binary.getParent());Files.writeString(binary,"must never execute");binary.toFile().setExecutable(true);
            rejects(()->dispatcher.execute(new JSONObject().put("task","ffmpeg_convert").put("data_b64","YQ==")
                    .put("ffmpeg_args",new JSONArray().put(";echo unsafe"))),"argumento ffmpeg bloqueado");
            return;
        }
        String[] expected=("ping health status diagnostic_basic worker_self_check network_probe endpoint_probe tailscale_status vps_assist_probe "
                +"emoji_recolor sha256 hash_batch text_stats log_extract log_summary log_digest zip zip_validate zip_audit maintenance_plan "
                +"ffmpeg_check ffprobe_check ffmpeg_convert ffprobe_media media_probe audio_convert tts_agent_status tts_agent_synthesize "
                +"tts_android_voices tts_atts_voices android_tts_voices tts_synthesize_benchmark tts_synthesize_piper tts_cache_lookup tts_cache_store "
                +"worker_logs boot_status service_status").split(" ");
        JSONArray announced=CoreWorkerDirectTaskExecutor.directSupportedTasks();Set<String> names=new HashSet<>();
        for(int i=0;i<announced.length();i++)check(names.add(announced.getString(i)),"duplicate announced task");
        check(names.equals(new HashSet<>(Arrays.asList(expected))),"catalog differs from original contract");
        JSONObject file=new JSONObject().put("name","hello.txt").put("text","hello");
        String zip=dispatcher.execute(new JSONObject().put("task","zip").put("files",new JSONArray().put(file))).getString("data_b64");
        for(String task:expected){
            check(CoreWorkerDirectTaskExecutor.supports(" "+task.toUpperCase(Locale.ROOT)+" "),"normalization lost");
            JSONObject body=new JSONObject().put("task",task).put("text","ERROR hello").put("emojis",new JSONArray())
                    .put("items",new JSONArray().put(new JSONObject().put("text","hello"))).put("files",new JSONArray().put(file))
                    .put("entries",new JSONArray()).put("targets",new JSONArray().put("invalid-local-target"));
            if(task.startsWith("zip_")||task.equals("tts_cache_store"))body.put("data_b64",zip);
            if(Set.of("ffmpeg_convert","audio_convert","ffprobe_media","media_probe").contains(task)){
                rejects(()->dispatcher.execute(body),"binários privados");continue;
            }
            JSONObject result=dispatcher.execute(body);
            check(result.has("ok")&&!result.optString("error").contains("handler")&&!result.optString("error").contains("não suportada"),"advertised task unreachable: "+task);
        }
    }
    static void builder(Context context,FakePreferences prefs)throws Exception{
        pair(prefs,"");
        File active=new File(context.getFilesDir(),"apk-self-builder/toolchain");active.mkdirs();
        Files.writeString(new File(active,"manifest.json").toPath(),"{}");
        CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1),refreshEntered=new CountDownLatch(1),refreshDone=new CountDownLatch(1);
        AtomicInteger smokes=new AtomicInteger();AtomicReference<Throwable> problem=new AtomicReference<>();
        Python.handler=(name,values)->{
            if(name.equals("preflight"))smokes.incrementAndGet();
            if(name.equals("run")){
                entered.countDown();try{await(release);}catch(Exception e){throw new RuntimeException(e);}
            }
            return new PyObject("{\"ok\":true,\"ready\":true}");
        };
        Thread build=new Thread(()->{try{JSONObject result=CoreWorkerApkBuildManager.execute(context,"apk_build_debug",new JSONObject(),"","local-test",1);check(result.optBoolean("ok"),"build gate failed: "+result);}catch(Throwable error){problem.set(error);}});
        Thread refresh=new Thread(()->{refreshEntered.countDown();try{CoreWorkerApkBuildManager.preflight(context,true);}catch(Throwable error){problem.set(error);}finally{refreshDone.countDown();}});
        try{
            build.start();await(entered);int before=smokes.get();refresh.start();await(refreshEntered);
            waitFor(()->refresh.getState()==Thread.State.BLOCKED||refreshDone.getCount()==0);
            check(refreshDone.getCount()==1&&smokes.get()==before,"toolchain refresh ran while Python build held it");
            check(before==1,"build provisioned again after approving its gate");
        }finally{release.countDown();build.join(5000);refresh.join(5000);}
        check(!build.isAlive()&&!refresh.isAlive()&&problem.get()==null,"build/refresh leaked: "+problem.get());
    }
    static void service(String scenario,Context context,FakePreferences prefs)throws Exception{
        CoreWorkerRuntimeService service=new CoreWorkerRuntimeService();service.preferences=prefs;service.root=context.root;
        if(scenario.equals("service_poll_rejected")){
            try{
                pair(prefs, "");set(service,"running",true);set(service,"jobExecutor",new CoreWorkerJobExecutor(context));
                ((ExecutorService)field(service,"agentExecutor")).shutdownNow();
                invoke(service,"pollJobs",new Class[]{String.class,boolean.class},"manual",true);
                check(!((AtomicBoolean)field(service,"pollRunning")).get(),"executor rejection left polling stuck");
            }finally{service.onDestroy();}return;
        }
        if(scenario.equals("service_lease_commit")){
            try{
                prefs.edit().putString("active_job_lease_owner","job#1").putLong("active_job_lease_local_deadline_ms",123L).commit();
                JSONObject active=new JSONObject().put("job_id","job").put("attempt",1);
                JSONObject response=new JSONObject().put("server_time",100).put("job",new JSONObject().put("lease_until",200));
                prefs.failNext=1;
                rejects(()->invoke(service,"rememberRenewedLease",new Class[]{JSONObject.class,JSONObject.class},active,response),"persistir");
                check(prefs.getLong("active_job_lease_local_deadline_ms",0)==123L&&prefs.disk.get("active_job_lease_local_deadline_ms").equals(123L),"failed lease commit extended ownership");
            }finally{service.onDestroy();}return;
        }
        CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);
        HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        server.createContext("/core-worker/heartbeat",exchange->{try{
            exchange.getRequestBody().readAllBytes();entered.countDown();release.await(5,TimeUnit.SECONDS);
            byte[] body="{\"ok\":true,\"direct_http_token\":\"late-token\"}".getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200,body.length);exchange.getResponseBody().write(body);
        }catch(Exception ignored){}finally{exchange.close();}});
        server.start();pair(prefs,"http://127.0.0.1:"+server.getAddress().getPort());set(service,"running",true);
        try{
            invoke(service,"reportHeartbeat",new Class[]{String.class},"manual-test");await(entered);
            if(scenario.equals("service_clear"))CoreWorkerRuntimeIdentity.clear(prefs);
            else service.onStartCommand(new Intent().setAction(CoreWorkerRuntimeService.ACTION_STOP),0,1);
            release.countDown();AtomicBoolean pending=(AtomicBoolean)field(service,"heartbeatRunning");waitFor(()->!pending.get());
            check(!prefs.getBoolean("agent_enabled",true)&&!prefs.contains("native_worker_last_heartbeat_at")&&!prefs.contains("direct_http_token"),"late heartbeat resurrected service identity");
        }finally{release.countDown();service.onDestroy();server.stop(0);}
    }
    static void updates(Context context,FakePreferences prefs)throws Exception{
        CountDownLatch firstEntered=new CountDownLatch(1),releaseFirst=new CountDownLatch(1);
        AtomicInteger requests=new AtomicInteger();
        HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);ExecutorService pool=Executors.newCachedThreadPool();server.setExecutor(pool);
        server.createContext("/core-worker/app/latest.json",exchange->{try{
            int number=requests.incrementAndGet();if(number==1){firstEntered.countDown();releaseFirst.await(5,TimeUnit.SECONDS);}
            byte[] body=("{\"versionCode\":999,\"versionName\":\"test\",\"notificationRequested\":true,\"notificationId\":\"cycle-"+number+"\"}").getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200,body.length);exchange.getResponseBody().write(body);
        }catch(Exception ignored){}finally{exchange.close();}});
        server.createContext("/core-worker/app/notification",exchange->{exchange.getRequestBody().readAllBytes();exchange.sendResponseHeaders(200,2);exchange.getResponseBody().write("{}".getBytes());exchange.close();});
        server.start();pair(prefs,"http://127.0.0.1:"+server.getAddress().getPort());
        CoreWorkerUpdateJobService service=new CoreWorkerUpdateJobService();service.root=context.root;service.preferences=prefs;
        JobParameters a=new JobParameters(),b=new JobParameters();Thread old=null;
        try{
            service.onStartJob(a);await(firstEntered);old=(Thread)field(service,"cycleThread");service.onStopJob(a);service.onStartJob(b);
            waitFor(()->service.finished.contains(b));releaseFirst.countDown();old.join(5000);
            check(!old.isAlive()&&service.finished.size()==1&&!service.finished.contains(a),"old generation finished another job");
            check(service.notifications.count==1&&prefs.getString("last_update_notification","").equals("cycle-2"),"old generation notified");
            check(service.polls==2,"duplicate or late polling");
        }finally{releaseFirst.countDown();service.onStopJob(b);if(old!=null)old.join(5000);server.stop(0);pool.shutdownNow();}
    }
}
