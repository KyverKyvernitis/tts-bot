package android.content;
public class Context {
    public static final int MODE_PRIVATE = 0;
    private final SharedPreferences prefs;
    public Context(SharedPreferences prefs) { this.prefs = prefs; }
    public SharedPreferences getSharedPreferences(String name, int mode) { return prefs; }
    public Context getApplicationContext() { return this; }
}
