package com.chaquo.python; public class PyObject {
 private final String value;
 public PyObject(){this.value="{}";}public PyObject(String value){this.value=value;}
 public PyObject callAttr(String name,Object... args){return Python.handler.apply(name,args);}
 public String toString(){return value;}
}
