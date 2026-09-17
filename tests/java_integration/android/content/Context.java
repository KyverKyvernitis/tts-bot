package android.content;
import java.io.File;
import android.content.pm.ApplicationInfo;
import android.content.res.AssetManager;
public class Context {
 public static final int MODE_PRIVATE=0;
 public static final String POWER_SERVICE="power", CONNECTIVITY_SERVICE="connectivity", NOTIFICATION_SERVICE="notification", JOB_SCHEDULER_SERVICE="job";
 public SharedPreferences preferences;
 public File root;
 public int starts, polls;
 public android.app.NotificationManager notifications = new android.app.NotificationManager();
 public Context() { }
 public Context(SharedPreferences prefs, File root) { this.preferences=prefs;this.root=root; }
 public Context getApplicationContext(){return this;}
 public SharedPreferences getSharedPreferences(String name,int mode){return preferences;}
 public File getFilesDir(){File dir=new File(root,"files");dir.mkdirs();return dir;}
 public File getCacheDir(){File dir=new File(root,"cache");dir.mkdirs();return dir;}
 public ApplicationInfo getApplicationInfo(){ApplicationInfo info=new ApplicationInfo();info.nativeLibraryDir=new File(root,"native").toString();info.dataDir=root.toString();return info;}
 public AssetManager getAssets(){return new AssetManager();}
 public Object getSystemService(String name){return NOTIFICATION_SERVICE.equals(name)?notifications:null;}
 public Intent registerReceiver(Object receiver,IntentFilter filter){return null;}
 public int checkSelfPermission(String permission){return 0;}
 public ComponentName startService(Intent intent){starts++;if(intent.getAction().contains("POLL"))polls++;return null;}
 public ComponentName startForegroundService(Intent intent){return startService(intent);}
 public String getPackageName(){return "dev.core.worker";}
}
