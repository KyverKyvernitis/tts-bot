package android.app; public class Notification {
 public static final String CATEGORY_SERVICE="service";
 public static class Builder {
  public Builder(android.content.Context c){} public Builder(android.content.Context c,String channel){}
  public Builder setSmallIcon(int i){return this;} public Builder setContentTitle(String s){return this;}
  public Builder setContentText(String s){return this;} public Builder setContentIntent(PendingIntent p){return this;}
  public Builder setAutoCancel(boolean b){return this;} public Builder setOngoing(boolean b){return this;}
  public Builder setCategory(String s){return this;} public Builder setOnlyAlertOnce(boolean b){return this;}
  public Builder setShowWhen(boolean b){return this;}
  public Notification build(){return new Notification();}
 }
}
