package com.soultide.localservice;

import android.content.Context;
import android.content.Intent;
import android.content.res.AssetFileDescriptor;
import android.net.Uri;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Account import/export controller for the service APK.
 *
 * Split from the single-APK MobileBootstrap helper: file selection is done by
 * the caller, this class owns private-dir paths, background copy, atomic
 * rename, progress JSON parsing and account control-file requests. Resource
 * packs are intentionally not handled here: the game owns its external path.
 * All file IO runs on worker threads; the UI thread never copies or verifies.
 */
public final class ImportController {

    public static final String ACCOUNT_PACK_NAME = "account.soulaccount";
    public static final String EXPORT_NAME = "current-account.soulaccount";

    private static final String PROGRESS_NAME = "mobile-import-progress.json";
    private static final String CONTROL_NAME = "mobile-control.json";
    private static final String LOG_NAME = "mobile-bootstrap.log";
    private static final String TAG = "SoulTideLocal";
    private static final Pattern JSON_STRING = Pattern.compile("\\\"%s\\\"\\s*:\\s*\\\"([^\\\"]*)\\\"");
    private static final Pattern JSON_NUMBER = Pattern.compile("\\\"%s\\\"\\s*:\\s*(\\d+)");
    private static final long COPY_CHUNK = 1024 * 1024;

    private ImportController() {}

    public static final class Progress {
        public final String stage;
        public final String message;
        public final String currentPath;
        public final String error;
        public final long totalFiles;
        public final long completedFiles;
        public final long totalBytes;
        public final long completedBytes;

        Progress(String stage, String message, String currentPath, String error,
                 long totalFiles, long completedFiles, long totalBytes, long completedBytes) {
            this.stage = stage;
            this.message = message;
            this.currentPath = currentPath;
            this.error = error;
            this.totalFiles = totalFiles;
            this.completedFiles = completedFiles;
            this.totalBytes = totalBytes;
            this.completedBytes = completedBytes;
        }
    }

    public interface CopyListener {
        void onProgress(long copied, long total);
        void onDone(File destination);
        void onError(Throwable error);
    }

    /** True when the import progress file reports an active importing stage. */
    public static boolean isImportActive(Progress progress) {
        if (progress == null) return false;
        String stage = progress.stage;
        return "account".equals(stage) || "switching".equals(stage)
                || "unlocking".equals(stage)
                || "starting".equals(stage);
    }

    public static boolean isFailed(Progress progress) {
        return progress != null && "failed".equals(progress.stage);
    }

    public static File dataDir(Context context) {
        return new File(context.getFilesDir(), "mobile-data");
    }

    public static File importsDir(Context context) {
        return new File(dataDir(context), "imports");
    }

    public static File progressFile(Context context) {
        return new File(dataDir(context), PROGRESS_NAME);
    }

    public static File controlFile(Context context) {
        return new File(dataDir(context), CONTROL_NAME);
    }

    public static File exportFile(Context context) {
        return new File(dataDir(context), "exports/" + EXPORT_NAME);
    }

    public static File pendingAccountPack(Context context) {
        return new File(importsDir(context), ACCOUNT_PACK_NAME);
    }

    /** Copy a user-selected document into the app private imports dir. */
    public static void copySelection(final Context context, final Uri uri,
                                     final String name, final CopyListener listener) {
        Thread worker = new Thread(() -> {
            try {
                File imports = importsDir(context);
                if (!imports.exists() && !imports.mkdirs()) {
                    throw new IOException("无法创建导入目录");
                }
                File destination = new File(imports, name);
                File temporary = new File(imports, "." + name + ".part");
                if (temporary.exists() && !temporary.delete()) {
                    throw new IOException("无法清理上次未完成的导入文件");
                }
                long total = uriLength(context, uri);
                long copied = 0L;
                try (InputStream input = context.getContentResolver().openInputStream(uri);
                     FileOutputStream output = new FileOutputStream(temporary)) {
                    if (input == null) throw new IOException("无法读取所选文件");
                    byte[] buffer = new byte[(int) COPY_CHUNK];
                    int read;
                    while ((read = input.read(buffer)) >= 0) {
                        if (read <= 0) continue;
                        output.write(buffer, 0, read);
                        copied += read;
                        listener.onProgress(copied, total);
                    }
                } catch (IOException error) {
                    temporary.delete();
                    throw error;
                }
                if (destination.exists() && !destination.delete()) {
                    temporary.delete();
                    throw new IOException("无法替换旧的导入文件");
                }
                if (!temporary.renameTo(destination)) {
                    temporary.delete();
                    throw new IOException("无法保存导入文件");
                }
                listener.onDone(destination);
            } catch (Throwable error) {
                listener.onError(error);
            }
        }, "soultide-" + name + "-copy");
        worker.setDaemon(true);
        worker.start();
    }

    /** Atomic request for the Python backend to export the current account. */
    public static void requestAccountExport(Context context) throws IOException {
        File data = dataDir(context);
        if (!data.exists() && !data.mkdirs()) throw new IOException("无法创建本地数据目录");
        File export = exportFile(context);
        if (export.isFile() && !export.delete()) throw new IOException("无法清理上次账号导出文件");
        File control = controlFile(context);
        File temporary = new File(data, "." + CONTROL_NAME + ".part");
        try (FileWriter writer = new FileWriter(temporary, false)) {
            writer.write("{\"action\":\"export_account\"}\n");
        }
        if (control.exists() && !control.delete()) throw new IOException("无法替换账号导出请求");
        if (!temporary.renameTo(control)) throw new IOException("无法提交账号导出请求");
    }

    /** Atomically request a database-only full skin/item unlock for the current account. */
    public static void requestEntitlementUnlock(Context context) throws IOException {
        File data = dataDir(context);
        if (!data.exists() && !data.mkdirs()) throw new IOException("无法创建本地数据目录");
        File control = controlFile(context);
        File temporary = new File(data, "." + CONTROL_NAME + ".part");
        try (FileWriter writer = new FileWriter(temporary, false)) {
            writer.write("{\"action\":\"unlock_entitlements\"}\n");
        }
        if (control.exists() && !control.delete()) throw new IOException("无法替换账号操作请求");
        if (!temporary.renameTo(control)) {
            temporary.delete();
            throw new IOException("无法提交账号全解锁请求");
        }
    }

    /** Read the atomic import progress file; null when absent or unreadable. */
    public static Progress readProgress(Context context) {
        File path = progressFile(context);
        if (!path.isFile()) return null;
        try (FileInputStream input = new FileInputStream(path);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) output.write(buffer, 0, read);
            }
            String json = output.toString("UTF-8");
            return new Progress(
                    jsonString(json, "stage"),
                    jsonString(json, "message"),
                    jsonString(json, "currentPath"),
                    jsonString(json, "error"),
                    jsonNumber(json, "totalFiles"),
                    jsonNumber(json, "completedFiles"),
                    jsonNumber(json, "totalBytes"),
                    jsonNumber(json, "completedBytes"));
        } catch (Exception ignored) {
            return null;
        }
    }

    /** Copy the backend-generated export to a user-chosen document uri. */
    public static void saveExportToUri(final Context context, final Uri destination,
                                       final Runnable done, final java.util.function.Consumer<Throwable> error) {
        Thread worker = new Thread(() -> {
            File source = exportFile(context);
            try (InputStream input = new FileInputStream(source);
                 OutputStream output = context.getContentResolver().openOutputStream(destination)) {
                if (output == null) throw new IOException("无法打开账号备份保存位置");
                byte[] buffer = new byte[(int) COPY_CHUNK];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read <= 0) continue;
                    output.write(buffer, 0, read);
                }
                done.run();
            } catch (Throwable failure) {
                error.accept(failure);
            }
        }, "soultide-account-export-copy");
        worker.setDaemon(true);
        worker.start();
    }

    /** Open the system document picker for a payload file. */
    public static Intent openDocument(String mime) {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(mime);
        return intent;
    }

    /** Open the system "save as" picker for the account export. */
    public static Intent createDocument(String mime, String title) {
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(mime);
        intent.putExtra(Intent.EXTRA_TITLE, title);
        return intent;
    }

    public static void trace(Context context, String message) {
        Log.i(TAG, message);
        try {
            File data = dataDir(context);
            if (!data.exists() && !data.mkdirs()) return;
            try (FileWriter writer = new FileWriter(new File(data, LOG_NAME), true)) {
                writer.write(System.currentTimeMillis() + " " + message + "\n");
            }
        } catch (IOException ignored) {
            Log.w(TAG, "cannot write bootstrap log", ignored);
        }
    }

    public static String formatBytes(long value) {
        if (value < 1024L * 1024L) return (value / 1024L) + " KB";
        if (value < 1024L * 1024L * 1024L) return (value / (1024L * 1024L)) + " MB";
        return String.format(java.util.Locale.US, "%.2f GB", value / (1024.0 * 1024.0 * 1024.0));
    }

    private static long uriLength(Context context, Uri uri) {
        try {
            AssetFileDescriptor descriptor = context.getContentResolver().openAssetFileDescriptor(uri, "r");
            if (descriptor == null) return -1L;
            long length = descriptor.getLength();
            descriptor.close();
            return length;
        } catch (Exception ignored) {
            return -1L;
        }
    }

    private static String jsonString(String json, String key) {
        Matcher matcher = Pattern.compile(String.format(JSON_STRING.pattern(), Pattern.quote(key))).matcher(json);
        return matcher.find() ? matcher.group(1).replace("\\n", "\n").replace("\\\"", "\"").replace("\\\\", "\\") : "";
    }

    private static long jsonNumber(String json, String key) {
        Matcher matcher = Pattern.compile(String.format(JSON_NUMBER.pattern(), Pattern.quote(key))).matcher(json);
        try {
            return matcher.find() ? Long.parseLong(matcher.group(1)) : 0L;
        } catch (NumberFormatException ignored) {
            return 0L;
        }
    }
}
