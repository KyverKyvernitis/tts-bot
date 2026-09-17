package android.util;
public final class Base64 {
    public static byte[] decode(String text, int flags) {
        return ((flags & URL_SAFE) != 0 ? java.util.Base64.getUrlDecoder() : java.util.Base64.getDecoder())
                .decode(text.replaceAll("\\s", ""));
    }
    public static final int DEFAULT = 0;
    public static final int NO_WRAP = 2, URL_SAFE = 8;
    public static String encodeToString(byte[] bytes, int flags) {
        return ((flags & URL_SAFE) != 0 ? java.util.Base64.getUrlEncoder() : java.util.Base64.getEncoder()).encodeToString(bytes);
    }
}
