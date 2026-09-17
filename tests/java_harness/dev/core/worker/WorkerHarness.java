package dev.core.worker;

import android.content.Context;
import com.sun.net.httpserver.HttpServer;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

public final class WorkerHarness {
    interface Checked { void run() throws Exception; }
    static void check(boolean condition, String detail) { if (!condition) throw new AssertionError(detail); }
    static void fails(Checked test) throws Exception {
        try { test.run(); } catch (Exception expected) { return; }
        throw new AssertionError("expected rejection");
    }
    static void await(CountDownLatch latch) throws Exception { check(latch.await(4, TimeUnit.SECONDS), "latch timed out"); }
    static void concurrent(int count, Checked action) throws Exception {
        ExecutorService pool = Executors.newFixedThreadPool(count);
        CountDownLatch go = new CountDownLatch(1);
        List<Future<?>> futures = new ArrayList<>();
        try {
            for (int i=0;i<count;i++) futures.add(pool.submit(() -> { await(go); action.run(); return null; }));
            go.countDown();
            for (Future<?> future : futures) future.get(5, TimeUnit.SECONDS);
        } finally { pool.shutdownNow(); }
    }
    static void pair(FakePreferences prefs) {
        CoreWorkerRuntimeIdentity.markDedicatedApkPair(prefs, prefs.edit()
                .putString("worker_id", "apk-test").putString("worker_token", "token")
                .putBoolean("agent_enabled", true), "apk-test");
    }
    static JSONObject enrollment(Context context) throws Exception {
        return new JSONObject().put("challenge", CoreWorkerAutoEnrollment.status(context).getString("challenge"))
                .put("parent_worker_id", "phone-test").put("worker_id", "phone-test-apk")
                .put("token", "child-token-abcdefghijklmnopqrstuvwxyz").put("server_url", "https://saved.invalid");
    }
    public static void main(String[] args) throws Exception {
        String which = args[0];
        if (which.equals("tar_external")) {
            try (InputStream input = new java.util.zip.GZIPInputStream(new FileInputStream(args[1]))) {
                CoreLinuxRootfsTarExtractor.extractTar(input, new File(args[2]));
            }
            return;
        }
        if (which.startsWith("recovery_")) { RecoveryHarness.run(which.substring(9)); return; }
        if (which.startsWith("process_")) { ProcessHarness.run(which.substring(8)); return; }
        if (which.startsWith("native_")) { NativeCallsHarness.run(which.substring(7)); return; }
        if (which.startsWith("fs_")) { FilesystemHarness.run(which.substring(3)); return; }
        FakePreferences prefs = new FakePreferences();
        Context context = new Context(prefs);
        switch (which) {
            case "install_concurrent": {
                Set<String> ids = ConcurrentHashMap.newKeySet();
                concurrent(24, () -> ids.add(CoreWorkerRuntimeIdentity.installId(prefs)));
                check(ids.size()==1 && prefs.commits==1, "install_id split or redundant commits");
                check(ids.contains(prefs.disk.get("install_id")), "returned ID is not durable");
                break;
            }
            case "install_commit_failure": {
                prefs.failNext=1;
                fails(() -> CoreWorkerRuntimeIdentity.installId(prefs));
                String id=CoreWorkerRuntimeIdentity.installId(prefs);
                check(id.equals(prefs.disk.get("install_id")), "retry returned unpersisted ID");
                break;
            }
            case "migration_idempotent": {
                prefs.edit().putString("worker_id","phone-parent").commit();
                CoreWorkerRuntimeIdentity.migrate(context);
                int count=prefs.commits;
                concurrent(20, () -> CoreWorkerRuntimeIdentity.migrate(context));
                check(prefs.commits==count, "migration writes on every status call");
                check("phone-parent-apk".equals(CoreWorkerRuntimeIdentity.runtimeWorkerId(context)), "parent/child collision");
                check(CoreWorkerRuntimeIdentity.directHttpPort(context)==8767, "child port");
                break;
            }
            case "pair_atomic": {
                JSONObject payload=enrollment(context);
                int before=prefs.commits;
                CoreWorkerAutoEnrollment.complete(context,payload);
                check(prefs.commits==before+1,"pair uses multiple transactions");
                check("phone-test-apk".equals(prefs.disk.get("runtime_worker_id")),"child missing on disk");
                check(payload.getString("token").equals(prefs.disk.get("worker_token")),"token missing on disk");
                check("phone-test".equals(prefs.disk.get("worker_id")),"canonical changed");
                check(CoreWorkerRuntimeService.starts==1 && CoreWorkerRuntimeService.polls==1,"start count");
                break;
            }
            case "pair_failure": {
                JSONObject payload=enrollment(context); prefs.failNext=1;
                fails(() -> CoreWorkerAutoEnrollment.complete(context,payload));
                check(CoreWorkerRuntimeService.starts==0 && CoreWorkerApkBuildManager.refreshes==0,"uncommitted pairing started agent");
                check(prefs.getString("worker_token", "").isEmpty(),"failed token visible in memory");
                check(!prefs.getBoolean("agent_enabled",false),"agent enabled after failed commit");
                break;
            }
            case "challenge_concurrent": {
                Set<String> values=ConcurrentHashMap.newKeySet();
                concurrent(24, () -> values.add(CoreWorkerAutoEnrollment.status(context).getString("challenge")));
                check(values.size()==1 && values.contains(prefs.disk.get("auto_enrollment_challenge")),"nonce split or not durable");
                break;
            }
            case "challenge_failure": {
                CoreWorkerRuntimeIdentity.installId(prefs); prefs.failNext=1;
                fails(() -> CoreWorkerAutoEnrollment.status(context));
                String value=CoreWorkerAutoEnrollment.status(context).getString("challenge");
                check(value.equals(prefs.disk.get("auto_enrollment_challenge")),"unpersisted nonce returned after commit failure");
                break;
            }
            case "clear_stale_heartbeat": case "stop_stale_heartbeat": {
                pair(prefs); long generation=CoreWorkerRuntimeIdentity.generation();
                if(which.startsWith("clear")) CoreWorkerRuntimeIdentity.clear(prefs);
                else CoreWorkerRuntimeIdentity.stopAgent(prefs,prefs.edit());
                check(!CoreWorkerRuntimeIdentity.isCurrent(prefs,generation,"token","apk-test"),"late heartbeat accepted");
                check(!prefs.getBoolean("agent_enabled", true),"agent still enabled");
                pair(prefs);
                check(!CoreWorkerRuntimeIdentity.isCurrent(prefs,generation,"token","apk-test"),"old generation revived on same-token re-pair");
                break;
            }
            case "listener_restart": case "listener_failure_recovery": {
                CoreWorkerSocketListener listener=new CoreWorkerSocketListener("harness-listener",1,1);
                AtomicInteger failures=new AtomicInteger(); CountDownLatch failed=new CountDownLatch(1);
                ServerSocket first=new ServerSocket(0);
                listener.start(first,1000,s -> { }, e -> { failures.incrementAndGet(); failed.countDown(); });
                if(which.contains("failure")) { first.close(); await(failed); }
                else listener.stop();
                ServerSocket second=new ServerSocket(0); CountDownLatch accepted=new CountDownLatch(1);
                listener.start(second,1000,s -> accepted.countDown(), e -> failures.incrementAndGet());
                try(Socket ignored=new Socket("127.0.0.1",second.getLocalPort())) { await(accepted); }
                check(listener.isRunning(),"old generation stopped new socket");
                listener.stop(); check(!listener.isRunning(),"stop state");
                break;
            }
            case "listener_bounded": {
                CoreWorkerSocketListener listener=new CoreWorkerSocketListener("harness-bounded",1,1);
                ServerSocket bound=new ServerSocket(0); CountDownLatch active=new CountDownLatch(1), release=new CountDownLatch(1);
                AtomicInteger executing=new AtomicInteger();
                listener.start(bound,1000,s -> { executing.incrementAndGet(); active.countDown(); try{release.await();}catch(InterruptedException ignored){} },e ->{});
                List<Socket> clients=new ArrayList<>();
                try {
                    clients.add(new Socket("127.0.0.1",bound.getLocalPort())); await(active);
                    clients.add(new Socket("127.0.0.1",bound.getLocalPort()));
                    Socket rejected=new Socket("127.0.0.1",bound.getLocalPort()); clients.add(rejected); rejected.setSoTimeout(3000);
                    check(rejected.getInputStream().read()==-1,"overflow client not closed");
                    check(executing.get()==1,"worker pool exceeded bound");
                    listener.stop();
                    for(Socket client:clients){client.setSoTimeout(3000);check(client.getInputStream().read()==-1,"client leaked on stop");}
                } finally { release.countDown(); listener.stop(); for(Socket client:clients)client.close(); }
                break;
            }
            case "direct_restart": {
                pair(prefs); int port;
                try(ServerSocket free=new ServerSocket(0)){port=free.getLocalPort();}
                prefs.edit().putInt("direct_http_port",port).putString("direct_http_token","token").commit();
                CoreWorkerDirectHttpServer server=new CoreWorkerDirectHttpServer(context,prefs,new NativeTtsManager());
                try {
                    for(int i=0;i<3;i++){
                        server.start();check(server.isRunning(),"not started");
                        check(get("http://127.0.0.1:"+port+"/health","token").contains("true"),"health failed");
                        server.stop();
                    }
                } finally {server.stop();}
                break;
            }
            case "tts_bind_failure": {
                try(ServerSocket occupied=new ServerSocket(0,1,InetAddress.getByName("127.0.0.1"))){
                    LocalNativeTtsHttpServer server=new LocalNativeTtsHttpServer(new NativeTtsManager(),occupied.getLocalPort());
                    fails(server::start);check(!server.isRunning(),"reported active before bind");server.stop();
                } break;
            }
            case "tts_restart": {
                int port;try(ServerSocket free=new ServerSocket(0)){port=free.getLocalPort();}
                LocalNativeTtsHttpServer server=new LocalNativeTtsHttpServer(new NativeTtsManager(),port);
                try {for(int i=0;i<3;i++){server.start();check(get("http://127.0.0.1:"+port+"/native-tts/status",null).contains("true"),"tts status");server.stop();}}
                finally{server.stop();}break;
            }
            case "http_bounded": {
                class Closing extends ByteArrayInputStream {boolean closed;Closing(byte[] b){super(b);}public void close(){closed=true;}}
                Closing exact=new Closing(new byte[31]);check(CoreWorkerHttpTransport.readBounded(exact,31).length()==31 && exact.closed,"exact limit");
                Closing excess=new Closing(new byte[32]);fails(()->CoreWorkerHttpTransport.readBounded(excess,31));check(excess.closed,"overflow stream leaked");
                prefs.edit().putString("server_url","https://saved.invalid/").commit();
                check("https://saved.invalid".equals(CoreWorkerHttpTransport.serverUrl(prefs,"https://build.invalid")),"saved URL ignored");
                break;
            }
            case "http_redirect": {
                AtomicInteger leaked=new AtomicInteger(); HttpServer server=server();
                server.createContext("/redirect",e->{e.getResponseHeaders().add("Location","/secret");e.sendResponseHeaders(302,-1);e.close();});
                server.createContext("/secret",e->{leaked.incrementAndGet();e.sendResponseHeaders(200,-1);e.close();});server.start();
                try{CoreWorkerHttpTransport.Result response=CoreWorkerHttpTransport.request("GET",base(server)+"/redirect",null,"secret-token",1000,1000);check(response.status==302 && leaked.get()==0,"redirect followed");}
                finally{server.stop(0);}break;
            }
            case "update_origin": {
                String root="https://example.invalid:443";
                check(CoreWorkerUpdateArtifacts.resolve(root,"/app.apk").getPath().equals("/app.apk"),"relative URL");
                for(String bad:List.of("http://example.invalid/app.apk","https://example.invalid:444/app.apk","https://other.invalid/app.apk","//other.invalid/app.apk","https://x@example.invalid/app.apk","https://example.invalid/app.apk#f")) fails(()->CoreWorkerUpdateArtifacts.resolve(root,bad));
                break;
            }
            case "fcm_latest_wins": {
                CountDownLatch first=new CountDownLatch(1), release=new CountDownLatch(1), last=new CountDownLatch(1);
                List<Integer> order=Collections.synchronizedList(new ArrayList<>());
                CoreWorkerBackgroundIo.latestRegistration(()->{order.add(0);first.countDown();try{release.await();}catch(InterruptedException ignored){}});await(first);
                for(int i=1;i<=100;i++){final int value=i;CoreWorkerBackgroundIo.latestRegistration(()->{order.add(value);if(value==100)last.countDown();});}
                release.countDown();await(last);check(order.equals(List.of(0,100)),"pending registrations not coalesced");break;
            }
            default: update(which);
        }
        System.out.println("PASS " + which);
    }
    static HttpServer server() throws Exception {return HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);}
    static String base(HttpServer server){return "http://127.0.0.1:"+server.getAddress().getPort();}
    static String get(String url,String token)throws Exception{return CoreWorkerHttpTransport.request("GET",url,null,token,1000,2000).body;}
    static void update(String which)throws Exception {
        Path directory=Files.createTempDirectory("worker-update-");File target=directory.resolve("update.apk").toFile();
        byte[] old="previous valid apk".getBytes(StandardCharsets.UTF_8), bytes="new valid apk".getBytes(StandardCharsets.UTF_8);
        Files.write(target.toPath(),bytes);String hash=CoreWorkerUpdateArtifacts.sha256(target);Files.write(target.toPath(),old);
        HttpServer server=server();
        server.createContext("/apk",e->{
            if(which.equals("update_redirect")){e.getResponseHeaders().add("Location","http://127.0.0.1:1/escaped");e.sendResponseHeaders(302,-1);e.close();return;}
            long length=which.equals("update_chunked_size")?0:which.equals("update_partial")?bytes.length+10:bytes.length;
            e.sendResponseHeaders(200,length);try{e.getResponseBody().write(bytes);}finally{e.close();}
        });server.start();
        try {
            String expected=which.equals("update_hash")?"b".repeat(64):hash;
            long limit=which.contains("size")?bytes.length-1:1024;
            Checked download=()->CoreWorkerUpdateArtifacts.download(base(server),"/apk",expected,target,null,limit);
            if(which.equals("update_success")){download.run();check(Arrays.equals(bytes,Files.readAllBytes(target.toPath())),"new APK missing");}
            else {fails(download);check(Arrays.equals(old,Files.readAllBytes(target.toPath())),"previous valid APK changed on failure");}
            check(!Files.exists(directory.resolve("update.apk.download")),"partial leaked");
        }finally{server.stop(0);Files.deleteIfExists(target.toPath());Files.deleteIfExists(directory.resolve("update.apk.download"));Files.delete(directory);}
    }
}
