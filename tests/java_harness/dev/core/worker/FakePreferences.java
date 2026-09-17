package dev.core.worker;

import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

final class FakePreferences implements SharedPreferences {
    final Map<String, Object> memory = new HashMap<>(), disk = new HashMap<>();
    int commits, failNext;
    @Override public synchronized Map<String, ?> getAll() { return new HashMap<>(memory); }
    @Override public synchronized String getString(String k, String f) { return (String) memory.getOrDefault(k, f); }
    @Override public synchronized float getFloat(String k, float f) { return (Float) memory.getOrDefault(k, f); }
    @Override public synchronized int getInt(String k, int f) { return (Integer) memory.getOrDefault(k, f); }
    @Override public synchronized long getLong(String k, long f) { return (Long) memory.getOrDefault(k, f); }
    @Override public synchronized boolean getBoolean(String k, boolean f) { return (Boolean) memory.getOrDefault(k, f); }
    @Override public synchronized boolean contains(String k) { return memory.containsKey(k); }
    @Override public Editor edit() { return new Edit(); }
    private final class Edit implements Editor {
        final Map<String, Object> pending = new HashMap<>();
        public Editor putString(String k, String v) { pending.put(k, v); return this; }
        public Editor putFloat(String k, float v) { pending.put(k, v); return this; }
        public Editor putInt(String k, int v) { pending.put(k, v); return this; }
        public Editor putLong(String k, long v) { pending.put(k, v); return this; }
        public Editor putBoolean(String k, boolean v) { pending.put(k, v); return this; }
        public Editor remove(String k) { pending.put(k, null); return this; }
        public void apply() { commit(); }
        public boolean commit() {
            synchronized (FakePreferences.this) {
                commits++;
                for (Map.Entry<String, Object> entry : pending.entrySet()) {
                    if (entry.getValue() == null) memory.remove(entry.getKey());
                    else memory.put(entry.getKey(), entry.getValue());
                }
                // Android changes its in-memory map even when disk commit fails.
                if (failNext > 0) { failNext--; return false; }
                disk.clear(); disk.putAll(memory);
                return true;
            }
        }
    }
}
