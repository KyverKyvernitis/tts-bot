package android.content;
public class Intent {
 public static final String ACTION_BATTERY_CHANGED="battery";
 public static final int FLAG_ACTIVITY_SINGLE_TOP=1, FLAG_ACTIVITY_CLEAR_TOP=2, FLAG_ACTIVITY_NEW_TASK=4;
 private String action=""; private java.util.Map<String,String> extras=new java.util.HashMap<>();
 public Intent(){} public Intent(Context context,Class<?> type){}
 public Intent setAction(String action){this.action=action;return this;}
 public String getAction(){return action;}
 public Intent putExtra(String key,String value){extras.put(key,value);return this;}
 public String getStringExtra(String key){return extras.get(key);}
 public int getIntExtra(String key,int fallback){return fallback;}
 public Intent setFlags(int flags){return this;}
}
