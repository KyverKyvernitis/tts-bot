package android.content.res; public class AssetManager {
 public static final int ACCESS_STREAMING=2;
 public java.io.InputStream open(String path)throws java.io.IOException{throw new java.io.FileNotFoundException(path);}
 public java.io.InputStream open(String path,int mode)throws java.io.IOException{return open(path);}
}
