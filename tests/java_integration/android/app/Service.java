package android.app; public class Service extends android.content.Context {
 public static final int START_STICKY=1,START_NOT_STICKY=2;
 public void onCreate(){} public void onDestroy(){} public int onStartCommand(android.content.Intent i,int f,int id){return 0;}
 public android.os.IBinder onBind(android.content.Intent i){return null;}
 public void startForeground(int id,Notification n){} public void stopForeground(boolean remove){} public void stopSelf(){}
}
