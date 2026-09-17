package android.app.job; public class JobService extends android.app.Service {
 public final java.util.List<JobParameters> finished=new java.util.concurrent.CopyOnWriteArrayList<>();
 public boolean onStartJob(JobParameters p){return false;}public boolean onStopJob(JobParameters p){return false;}
 public void jobFinished(JobParameters p,boolean again){finished.add(p);}
}
