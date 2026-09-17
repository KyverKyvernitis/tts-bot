package dev.core.worker;

import android.util.AtomicFile;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.zip.*;
import static dev.core.worker.WorkerHarness.*;

final class FilesystemHarness {
    static final String OLD="a".repeat(64), NEW="b".repeat(64);
    static void run(String test) throws Exception {
        Path root=Files.createTempDirectory("worker-filesystem-");
        try {
            if(test.startsWith("tar_")){tar(test,root);return;}
            File active=root.resolve("toolchain").toFile(),next=root.resolve("toolchain-next").toFile(),previous=root.resolve("toolchain-previous").toFile();
            FakePreferences prefs=new FakePreferences();
            switch(test){
                case "nofollow": {
                    Path outside=Files.createTempDirectory("worker-outside-");
                    try {
                        Files.writeString(outside.resolve("keep"),"keep");
                        Files.createDirectory(root.resolve("delete"));
                        Files.createSymbolicLink(root.resolve("delete/link"),outside);
                        CoreLinuxRootfsFilesystem.removeTree(root.resolve("delete").toFile());
                        check(Files.readString(outside.resolve("keep")).equals("keep"),"cleanup crossed symlink");
                    }finally{Files.deleteIfExists(outside.resolve("keep"));Files.delete(outside);}
                    break;
                }
                case "rootfs_promotion": {
                    Files.createDirectory(active.toPath());Files.writeString(active.toPath().resolve("old"),"old");
                    Files.createDirectory(next.toPath());Files.writeString(next.toPath().resolve("new"),"new");
                    CoreLinuxRootfsFilesystem.promote(active,next,previous);
                    check(new File(active,"new").isFile()&&new File(previous,"old").isFile(),"promotion lost previous");break;
                }
                case "rootfs_recovery": {
                    Files.createDirectory(previous.toPath());Files.writeString(previous.toPath().resolve("old"),"old");
                    check(CoreLinuxRootfsFilesystem.recover(active,previous),"did not recover interrupted promotion");
                    check(new File(active,"old").isFile(),"recovered wrong rootfs");break;
                }
                case "rootfs_missing_staging": {
                    Files.createDirectory(active.toPath());Files.writeString(active.toPath().resolve("keep"),"old");
                    Files.createDirectory(previous.toPath());Files.writeString(previous.toPath().resolve("keep"),"older");
                    fails(()->CoreLinuxRootfsFilesystem.promote(active,next,previous));
                    check(new File(active,"keep").isFile()&&new File(previous,"keep").isFile(),"missing staging destroyed installed directories");break;
                }
                case "atomic_roundtrip": {
                    File file=root.resolve("state.json").toFile();
                    CoreWorkerAtomicTextFiles.write(file,"α".repeat(4000));
                    check(new String(CoreWorkerAtomicTextFiles.read(file,8000),StandardCharsets.UTF_8).equals("α".repeat(4000)),"text truncated");
                    fails(()->CoreWorkerAtomicTextFiles.read(file,7999));
                    check(CoreWorkerAtomicTextFiles.readPrefix(file,17).length==17,"prefix semantics changed");break;
                }
                case "atomic_failure": {
                    File file=root.resolve("state.json").toFile();CoreWorkerAtomicTextFiles.write(file,"old");
                    AtomicFile.failNextWrite=true;fails(()->CoreWorkerAtomicTextFiles.write(file,"new"));
                    check(Files.readString(file.toPath()).equals("old"),"failed write replaced old text");
                    check(!Files.exists(Path.of(file+".new")),"failed text partial leaked");break;
                }
                case "atomic_recovery": {
                    File file=root.resolve("state.json").toFile();Files.writeString(Path.of(file+".bak"),"backup");
                    check(new String(CoreWorkerAtomicTextFiles.read(file,128),StandardCharsets.UTF_8).equals("backup"),"legacy backup not recovered");break;
                }
                case "atomic_short_reads": {
                    byte[] expected="payload with short reads".getBytes(StandardCharsets.UTF_8);
                    InputStream shortReads=new ByteArrayInputStream(expected){public synchronized int read(byte[] b,int o,int n){return super.read(b,o,Math.min(n,2));}};
                    check(Arrays.equals(CoreWorkerAtomicTextFiles.readFully(shortReads,1024,true),expected),"single-read truncation");break;
                }
                default: {
                    toolchain(active,OLD);CoreWorkerApkToolchainFilesystem.recover(root.toFile(),prefs);CoreWorkerApkToolchainFilesystem.confirm(root.toFile(),prefs);
                    if(test.equals("toolchain_crash_before_next")){
                        CoreLinuxRootfsFilesystem.move(active,previous);
                        prefs.edit().putString("apk_self_builder_pending_toolchain_fingerprint",NEW).commit();
                        CoreWorkerApkToolchainFilesystem.recover(root.toFile(),prefs);
                        check(CoreWorkerApkToolchainFilesystem.fingerprint(active).equals(OLD),"crash did not restore previous");
                        check(!prefs.contains("apk_self_builder_pending_toolchain_fingerprint"),"stale pending survived rollback");break;
                    }
                    toolchain(next,NEW);
                    if(test.equals("toolchain_intent_failure")){
                        prefs.failNext=1;fails(()->CoreWorkerApkToolchainFilesystem.promote(root.toFile(),active,next,prefs,NEW));
                        check(CoreWorkerApkToolchainFilesystem.fingerprint(active).equals(OLD),"failed intent still promoted");
                        CoreWorkerApkToolchainFilesystem.recover(root.toFile(),prefs);break;
                    }
                    if(test.equals("toolchain_crash_after_next")){
                        CoreLinuxRootfsFilesystem.move(active,previous);CoreLinuxRootfsFilesystem.move(next,active);
                        CoreWorkerApkToolchainFilesystem.recover(root.toFile(),prefs);
                        check(NEW.equals(prefs.disk.get("apk_self_builder_pending_toolchain_fingerprint")),"new directory not pending after crash");break;
                    }
                    CoreWorkerApkToolchainFilesystem.promote(root.toFile(),active,next,prefs,NEW);
                    if(test.equals("toolchain_rollback")){
                        CoreWorkerApkToolchainFilesystem.rollback(root.toFile(),prefs,"smoke failed");
                        check(OLD.equals(CoreWorkerApkToolchainFilesystem.fingerprint(active)),"wrong rollback");
                        check(new File(root.toFile(),"toolchain-failed/manifest.json").isFile(),"failed candidate not retained");
                        check(!prefs.disk.containsKey("apk_self_builder_pending_toolchain_fingerprint"),"pending not durably cleared");break;
                    }
                    if(test.equals("toolchain_confirm_failure")){
                        prefs.failNext=1;fails(()->CoreWorkerApkToolchainFilesystem.confirm(root.toFile(),prefs));
                        check(OLD.equals(prefs.disk.get("apk_self_builder_known_good_toolchain_fingerprint")),"failure reported durable good");
                        CoreWorkerApkToolchainFilesystem.recover(root.toFile(),prefs);
                        check(NEW.equals(prefs.disk.get("apk_self_builder_toolchain_fingerprint")),"failed commit was never retried");break;
                    }
                    check(test.equals("toolchain_success"),"unknown test "+test);
                    CoreWorkerApkToolchainFilesystem.confirm(root.toFile(),prefs);
                    check(NEW.equals(prefs.disk.get("apk_self_builder_known_good_toolchain_fingerprint")),"known-good not durable");
                    check(OLD.equals(CoreWorkerApkToolchainFilesystem.fingerprint(previous)),"previous lost");
                }
            }
        }finally{CoreLinuxRootfsFilesystem.removeTree(root.toFile());}
    }
    static void toolchain(File directory,String fingerprint)throws Exception{
        Files.createDirectory(directory.toPath());Files.writeString(directory.toPath().resolve("manifest.json"),"{}");
        CoreWorkerAtomicTextFiles.write(new File(directory,".release-fingerprint"),fingerprint+"\n");
    }
    static byte[] archive(String path,char type,String link,byte[] data)throws Exception{
        byte[] header=new byte[512];put(header,0,100,path);put(header,100,8,"0000755");put(header,124,12,String.format("%011o",data.length));
        header[156]=(byte)type;put(header,157,100,link);Arrays.fill(header,148,156,(byte)' ');
        long sum=0;for(byte b:header)sum+=b&255;put(header,148,8,String.format("%06o",sum));
        ByteArrayOutputStream out=new ByteArrayOutputStream();out.write(header);out.write(data);out.write(new byte[(512-data.length%512)%512]);return out.toByteArray();
    }
    static void put(byte[] header,int offset,int len,String value){byte[] data=value.getBytes(StandardCharsets.UTF_8);System.arraycopy(data,0,header,offset,Math.min(len,data.length));}
    static byte[] pax(String record){int length=record.getBytes(StandardCharsets.UTF_8).length+3;while(true){String out=length+" "+record+"\n";int actual=out.getBytes(StandardCharsets.UTF_8).length;if(actual==length)return out.getBytes(StandardCharsets.UTF_8);length=actual;}}
    static void tar(String test,Path root)throws Exception{
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();
        String path="usr/share/example", link="";char type='0';byte[] content="example".getBytes(StandardCharsets.UTF_8);
        CoreLinuxRootfsTarExtractor.Limits limits=new CoreLinuxRootfsTarExtractor.Limits(8,1024*1024,1024*1024);
        CoreLinuxRootfsTarExtractor.LinkCreator creator=(target,targetLink)->Files.createSymbolicLink(target,targetLink);
        switch(test){
            case "tar_traversal":path="../escape";break;
            case "tar_absolute":path="/escape";break;
            case "tar_hardlink":type='1';link="target";content=new byte[0];break;
            case "tar_symlink_external":type='2';link="../../../escape";content=new byte[0];break;
            case "tar_symlink_failure":type='2';link="target";content=new byte[0];creator=(a,b)->{throw new IOException("injected symlink denial");};break;
            case "tar_metadata_count":
                for(int i=0;i<9;i++)bytes.write(archive("pax",'g',"",pax("comment=x")));break;
            case "tar_metadata_size":type='g';content=new byte[65537];break;
            case "tar_pax_traversal":bytes.write(archive("pax",'x',"",pax("path=../escape")));break;
            case "tar_pax_unicode":bytes.write(archive("pax",'x',"",pax("path=usr/share/á")));break;
            case "tar_symlink_ok":type='2';link="target";content=new byte[0];break;
        }
        bytes.write(archive(path,type,link,content));bytes.write(new byte[1024]);
        byte[] raw=bytes.toByteArray();if(test.equals("tar_checksum"))raw[0]^=1;
        InputStream input=new ByteArrayInputStream(raw);
        if(test.equals("tar_gzip_truncated")){
            ByteArrayOutputStream zipped=new ByteArrayOutputStream();try(GZIPOutputStream gzip=new GZIPOutputStream(zipped)){gzip.write(raw);}
            byte[] compressed=zipped.toByteArray();input=new GZIPInputStream(new ByteArrayInputStream(Arrays.copyOf(compressed,compressed.length-6)));
        }
        final InputStream stream=input;final CoreLinuxRootfsTarExtractor.LinkCreator links=creator;
        Checked extract=()->CoreLinuxRootfsTarExtractor.extractTar(stream,root.toFile(),limits,links);
        if(Set.of("tar_normal","tar_pax_unicode","tar_symlink_ok").contains(test)){
            extract.run();
            if(test.equals("tar_pax_unicode"))check(Files.isRegularFile(root.resolve("usr/share/á")),"PAX UTF-8 path truncated");
            else if(test.equals("tar_symlink_ok"))check(Files.isSymbolicLink(root.resolve(path)),"safe symlink missing");
            else check(Files.readString(root.resolve(path)).equals("example"),"file content changed");
        }else fails(extract);
        stream.close();
    }
}
