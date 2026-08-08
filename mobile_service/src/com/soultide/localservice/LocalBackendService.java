package com.soultide.localservice;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.Process;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileReader;

import org.json.JSONObject;

/**
 * Foreground backend service (card 1 + card 3).
 *
 * Shows the persistent "本地服务运行中" notification and, since card 3,
 * boots the bundled Python runtime (assets/mobile-runtime) through
 * RuntimeController: extract -> environment -> native libs -> mobile_entry.py.
 * The service and the Python interpreter share one process; onDestroy exits
 * the process so no listener keeps running.
 */
public final class LocalBackendService extends Service {

    private static final int NOTIFICATION_ID = 1;
    private static volatile boolean running = false;

    public static boolean isRunning() {
        return running;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        running = true;
        PowerManager powerManager = (PowerManager) getSystemService(POWER_SERVICE);
        if (powerManager != null) {
            powerManager.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    "soultide:localservice").acquire();
        }
        startAsForeground();
        RuntimeController.startAsync(this);
    }

    private void startAsForeground() {
        Notification notification = buildNotification();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIFICATION_ID, notification);
        }
    }

    private Notification buildNotification() {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= 26) {
            builder = new Notification.Builder(this, LocalServiceActivity.channelId());
        } else {
            builder = new Notification.Builder(this);
        }
        Intent contentIntent = new Intent(this, LocalServiceActivity.class);
        PendingIntent pending = PendingIntent.getActivity(
                this, 0, contentIntent,
                Build.VERSION.SDK_INT >= 23
                        ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                        : PendingIntent.FLAG_UPDATE_CURRENT);
        return builder
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentTitle(getString(R.string.notification_title))
                .setContentText(getString(R.string.notification_text))
                .setContentIntent(pending)
                .setOngoing(true)
                .build();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        NotificationManager manager =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager != null) {
            manager.cancel(NOTIFICATION_ID);
        }
        RuntimeController.trace(this, "service destroyed; killing forked children then process");
        RuntimeController.stopNativeHttp();
        killForkedChildren();
        running = false;
        super.onDestroy();
        android.os.Process.killProcess(android.os.Process.myPid());
        System.exit(0);
    }

    /**
     * Python fork-server mode runs each server as its own child process of this
     * process. Killing only the main process would leave them orphaned with the
     * ports still listening, so first signal-kill every child (same UID,
     * PPid == this process) and then exit.
     */
    private void killForkedChildren() {
        File manifest = new File(
                new File(RuntimeController.runtimeRoot(this), "server"),
                "mobile-server-pids.json");
        boolean manifestRead = false;
        try (FileInputStream input = new FileInputStream(manifest)) {
            ByteArrayOutputStream output = new ByteArrayOutputStream();
            byte[] buffer = new byte[4096];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) output.write(buffer, 0, read);
            }
            JSONObject servers = new JSONObject(output.toString("UTF-8"))
                    .optJSONObject("servers");
            if (servers != null) {
                manifestRead = true;
                for (String name : new String[] {"sdk", "http", "tcp"}) {
                    killPid(servers.optInt(name, -1));
                }
            }
        } catch (Exception error) {
            RuntimeController.trace(this, "PID manifest unavailable: " + error.getClass().getSimpleName());
        } finally {
            if (manifest.exists() && !manifest.delete()) {
                RuntimeController.trace(this, "cannot delete PID manifest");
            }
        }
        if (manifestRead) {
            return;
        }
        int myPid = Process.myPid();
        File[] dirs = new File("/proc").listFiles();
        if (dirs == null) {
            return;
        }
        for (File dir : dirs) {
            String name = dir.getName();
            if (name.isEmpty() || !Character.isDigit(name.charAt(0))) {
                continue;
            }
            int pid;
            try {
                pid = Integer.parseInt(name);
            } catch (NumberFormatException e) {
                continue;
            }
            if (pid == myPid) {
                continue;
            }
            BufferedReader reader = null;
            int ppid = -1;
            try {
                reader = new BufferedReader(new FileReader(new File(dir, "status")));
                String line;
                while ((line = reader.readLine()) != null) {
                    if (line.startsWith("PPid:")) {
                        ppid = Integer.parseInt(line.substring(5).trim());
                        break;
                    }
                }
            } catch (Exception e) {
                continue;
            } finally {
                if (reader != null) {
                    try {
                        reader.close();
                    } catch (Exception e) {
                        // ignore
                    }
                }
            }
            if (ppid == myPid) {
                RuntimeController.trace(this, "killing forked child pid=" + pid);
                try {
                    Runtime.getRuntime().exec(
                            new String[] { "/system/bin/kill", "-9", String.valueOf(pid) });
                } catch (Exception e) {
                    // best effort; process exit below still applies
                }
            }
        }
    }

    private void killPid(int pid) {
        if (pid <= 1 || pid == Process.myPid()) {
            return;
        }
        RuntimeController.trace(this, "killing recorded forked child pid=" + pid);
        try {
            java.lang.Process process = Runtime.getRuntime().exec(
                    new String[] {"/system/bin/kill", "-9", String.valueOf(pid)});
            process.waitFor();
        } catch (Exception error) {
            RuntimeController.trace(this, "cannot kill recorded child pid=" + pid);
        }
    }
}
