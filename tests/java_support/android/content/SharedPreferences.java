package android.content;

import java.util.Map;

/** JVM test double API; production is compiled against Android separately. */
public interface SharedPreferences {
    Map<String, ?> getAll();
    String getString(String key, String fallback);
    float getFloat(String key, float fallback);
    int getInt(String key, int fallback);
    long getLong(String key, long fallback);
    boolean getBoolean(String key, boolean fallback);
    boolean contains(String key);
    Editor edit();
    interface Editor {
        Editor putString(String key, String value);
        Editor putFloat(String key, float value);
        Editor putInt(String key, int value);
        Editor putLong(String key, long value);
        Editor putBoolean(String key, boolean value);
        Editor remove(String key);
        boolean commit();
        void apply();
    }
}
