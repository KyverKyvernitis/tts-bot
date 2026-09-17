package com.chaquo.python; public class Python {
 public static java.util.function.BiFunction<String,Object[],PyObject> handler=(name,args)->new PyObject("{\"ok\":true,\"ready\":true}");
 public static boolean isStarted(){return true;}public static void start(Object platform){}
 public static Python getInstance(){return new Python();}public PyObject getModule(String name){return new PyObject();}
}
