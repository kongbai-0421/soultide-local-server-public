package com.soultide.localservice;

import android.content.Context;
import android.util.Log;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;

import org.kivy.android.PythonService;

/**
 * Runtime lifecycle for the service APK (card 3).
 *
 * Extracts assets/mobile-runtime into app-private files, writes the runtime
 * environment contract (p4a_env_vars.txt, same variables as the desktop
 * runtime), loads the bundled native libraries in dependency order and starts
 * the Python entry point (mobile_entry.py) on a dedicated thread.  Health is
 * tracked against the three baseline ports (SDK/HTTP/TCP).
 */
public final class RuntimeController {

    private static final String TAG = "SoulTideLocal";
    private static final String RUNTIME_ASSET = "mobile-runtime";
    private static final String RUNTIME_VERSION = "20260808-native-http-053";
    private static final String LOG_NAME = "mobile-bootstrap.log";
    private static final String[] LIBRARIES = {
            "libsqlite3.so", "libffi.so", "libcr2.so", "libss2.so",
            "libpython3.14.so", "libp4a_.so"
    };

    public static final int SDK_PORT = 8000;
    public static final int HTTP_PORT = 8081;
    public static final int TCP_PORT = 51121;

    private static volatile boolean started;
    private static volatile boolean healthy;
    private static volatile Throwable failure;
    private static final Object lock = new Object();

    public static boolean isStarted() {
        return started;
    }

    public static boolean isBackendHealthy() {
        return healthy;
    }

    public static Throwable failure() {
        return failure;
    }

    public static void stopNativeHttp() {
        LocalHttpServer.stop();
    }

    public static File runtimeRoot(Context context) {
        return new File(context.getFilesDir(), RUNTIME_ASSET);
    }

    private RuntimeController() {}

    /** Starts runtime preparation, native bootstrap and the Python entry. */
    public static void startAsync(final Context context) {
        synchronized (lock) {
            if (started) {
                return;
            }
            started = true;
            healthy = false;
            failure = null;
        }
        Thread worker = new Thread(() -> {
            try {
                File runtime = prepareRuntime(context);
                ensureDatabase(context, runtime);
                writeRuntimeEnvironment(context, runtime);
                loadRuntimeLibraries(context);
                LocalHttpServer.start(context, runtime);
                trace(context, "native runtime loaded; starting Python entrypoint");
                Thread server = new Thread(() -> {
                    try {
                        PythonService.nativeStart(
                                runtime.getAbsolutePath(),
                                new File(runtime, "server").getAbsolutePath(),
                                new File(runtime, "server/tools/mobile_entry.py").getAbsolutePath(),
                                "soultide-local",
                                new File(runtime, "_python_bundle").getAbsolutePath(),
                                new File(runtime, "_python_bundle/stdlib.zip").getAbsolutePath()
                                        + ":" + new File(runtime, "_python_bundle/modules").getAbsolutePath(),
                                "");
                        trace(context, "Python entrypoint returned unexpectedly");
                    } catch (Throwable error) {
                        failure = error;
                        trace(context, "Python entrypoint failed: " + error);
                    }
                }, "soultide-local-server");
                server.setDaemon(true);
                server.start();
                watchHealth(context);
            } catch (Throwable error) {
                failure = error;
                trace(context, "runtime bootstrap failed: " + error);
            }
        }, "soultide-local-runtime");
        worker.setDaemon(true);
        worker.start();
    }

    private static void watchHealth(final Context context) {
        long deadline = System.currentTimeMillis() + 600000L;
        while (System.currentTimeMillis() < deadline) {
            if (failure != null) {
                healthy = false;
                return;
            }
            if (isBackendHealthyNow()) {
                healthy = true;
                trace(context, "backend health check passed (sdk=" + SDK_PORT
                        + " http=" + HTTP_PORT + " tcp=" + TCP_PORT + ")");
                return;
            }
            try {
                Thread.sleep(1000L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                return;
            }
        }
        failure = new IOException("本地服务启动超时；请查看 mobile-bootstrap.log 与 Python 输出。");
        healthy = false;
    }

    private static boolean isBackendHealthyNow() {
        boolean http = false;
        try {
            HttpURLConnection connection =
                    (HttpURLConnection) new URL("http://127.0.0.1:" + SDK_PORT + "/health").openConnection();
            connection.setConnectTimeout(300);
            connection.setReadTimeout(300);
            connection.setRequestMethod("GET");
            http = connection.getResponseCode() < 500;
            connection.disconnect();
        } catch (Exception ignored) {
            // not ready yet
        }
        boolean tcp = false;
        try (Socket socket = new Socket()) {
            socket.connect(new InetSocketAddress("127.0.0.1", TCP_PORT), 300);
            tcp = true;
        } catch (Exception ignored) {
            // not ready yet
        }
        return http && tcp;
    }

    private static File prepareRuntime(Context context) throws IOException {
        File runtime = runtimeRoot(context);
        File marker = new File(runtime, ".prepared");
        if (marker.isFile() && RUNTIME_VERSION.equals(readMarker(marker))) {
            trace(context, "runtime ready path=" + runtime.getAbsolutePath());
            return runtime;
        }
        trace(context, "extracting runtime asset=" + RUNTIME_ASSET + " path=" + runtime.getAbsolutePath());
        if (runtime.exists()) {
            deleteRecursively(runtime);
        }
        copyAssetTree(context, RUNTIME_ASSET, runtime);
        if (!new File(runtime, "_python_bundle/stdlib.zip").isFile()) {
            throw new IOException("安装包缺少内置 Android Python runtime");
        }
        writeMarker(marker, RUNTIME_VERSION);
        trace(context, "runtime extracted version=" + RUNTIME_VERSION);
        return runtime;
    }

    private static String readMarker(File marker) throws IOException {
        try (FileInputStream input = new FileInputStream(marker)) {
            byte[] buffer = new byte[128];
            int read = input.read(buffer);
            return read > 0 ? new String(buffer, 0, read, "UTF-8").trim() : "";
        }
    }

    private static void writeMarker(File marker, String value) throws IOException {
        try (FileOutputStream output = new FileOutputStream(marker, false)) {
            output.write(value.getBytes("UTF-8"));
        }
    }

    private static void deleteRecursively(File file) {
        File[] children = file.listFiles();
        if (children != null) {
            for (File child : children) {
                deleteRecursively(child);
            }
        }
        file.delete();
    }

    private static void copyAssetTree(Context context, String asset, File destination) throws IOException {
        String[] children = context.getAssets().list(asset);
        if (children == null || children.length == 0) {
            if (!destination.getParentFile().exists() && !destination.getParentFile().mkdirs()) {
                throw new IOException("无法创建 runtime 目录");
            }
            try (InputStream input = context.getAssets().open(asset);
                 FileOutputStream output = new FileOutputStream(destination)) {
                byte[] buffer = new byte[1024 * 1024];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read > 0) output.write(buffer, 0, read);
                }
            }
            if (destination.getName().endsWith(".so")) {
                destination.setReadable(true, true);
                if (!destination.setExecutable(true, true)) {
                    throw new IOException("无法设置运行库执行权限: " + destination.getName());
                }
            }
            return;
        }
        if (!destination.exists() && !destination.mkdirs()) {
            throw new IOException("无法创建 runtime 子目录");
        }
        for (String child : children) {
            copyAssetTree(context, asset + "/" + child, new File(destination, child));
        }
    }

    private static void writeRuntimeEnvironment(Context context, File runtime) throws IOException {
        File server = new File(runtime, "server");
        File data = new File(context.getFilesDir(), "mobile-data");
        if (!data.exists() && !data.mkdirs()) throw new IOException("无法创建本地数据目录");
        // The imported offline mirror is authoritative for loopback HTTP.
        // Do not point the server at the empty local-http staging directory:
        // Unity needs assetMap.clv and bundles from this complete mirror.
        File assetRoot = new File(data, "offline_cdn/Android");
        if (!assetRoot.exists() && !assetRoot.mkdirs()) throw new IOException("无法创建本地服务资源目录");
        File environment = new File(server, "p4a_env_vars.txt");
        BufferedWriter writer = new BufferedWriter(new FileWriter(environment, false));
        try {
            writer.write("SOULTIDE_ROOT=" + server.getAbsolutePath()); writer.newLine();
            writer.write("SOULTIDE_DATA_ROOT=" + data.getAbsolutePath()); writer.newLine();
            writer.write("SOULTIDE_ASSET_ROOT=" + assetRoot.getAbsolutePath()); writer.newLine();
            writer.write("SOULTIDE_DB_PATH=" + new File(data, "soultide.db").getAbsolutePath()); writer.newLine();
            writer.write("SOULTIDE_PYTHON=" + new File(context.getApplicationInfo().nativeLibraryDir, "libpythonbin.so").getAbsolutePath()); writer.newLine();
            writer.write("SOULTIDE_MOBILE_MODE=1"); writer.newLine();
            // MuMu's ARM translation layer can leave forked Python listeners
            // bound but unable to service their own loopback health checks.
            // The embedded runtime has an in-process thread mode specifically
            // for Android; it keeps all three listeners in the loaded Python
            // interpreter and avoids the 90-second startup timeout.
            writer.write("SOULTIDE_INPROCESS_SERVERS=1"); writer.newLine();
            writer.write("SOULTIDE_BIND_HOST=127.0.0.1"); writer.newLine();
            writer.write("SOULTIDE_SERVER_IP=127.0.0.1"); writer.newLine();
            writer.write("SOULTIDE_UPDATE_MODE=local"); writer.newLine();
            writer.write("SOULTIDE_ALLOW_UPSTREAM=0"); writer.newLine();
            writer.write("SOULTIDE_CDN_UPSTREAM_FALLBACK=0"); writer.newLine();
            writer.write("SOULTIDE_SDK_PORT=" + SDK_PORT); writer.newLine();
            writer.write("SOULTIDE_HTTP_PORT=" + HTTP_PORT); writer.newLine();
            writer.write("SOULTIDE_TCP_PORT=" + TCP_PORT); writer.newLine();
        } finally {
            writer.close();
        }
        trace(context, "runtime environment written to " + environment.getAbsolutePath());
    }

    private static void loadRuntimeLibraries(Context context) throws IOException {
        File nativeDir = new File(context.getApplicationInfo().nativeLibraryDir);
        for (String library : LIBRARIES) {
            File file = new File(nativeDir, library);
            if (!file.isFile()) {
                throw new IOException("缺少内置运行库: " + library);
            }
            System.load(file.getAbsolutePath());
        }
    }

    private static void ensureDatabase(Context context, File runtime) throws IOException {
        File data = new File(context.getFilesDir(), "mobile-data");
        if (!data.exists() && !data.mkdirs()) throw new IOException("无法创建本地数据目录");
        File target = new File(data, "soultide.db");
        if (target.isFile()) return;
        File template = new File(runtime, "server/soultide.db");
        if (!template.isFile()) throw new IOException("runtime 缺少 SQLite 初始数据库");
        try (FileInputStream input = new FileInputStream(template);
             FileOutputStream output = new FileOutputStream(target)) {
            byte[] buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) output.write(buffer, 0, read);
            }
        }
    }

    static void trace(Context context, String message) {
        Log.i(TAG, message);
        try {
            File data = new File(context.getFilesDir(), "mobile-data");
            if (!data.exists() && !data.mkdirs()) return;
            try (FileWriter writer = new FileWriter(new File(data, LOG_NAME), true)) {
                writer.write(System.currentTimeMillis() + " " + message + "\n");
            }
        } catch (IOException ignored) {
            Log.w(TAG, "cannot write bootstrap log", ignored);
        }
    }
}
