package com.soultide.localservice;

import android.app.Activity;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;

/**
 * Service APK UI (card 2).
 *
 * Shows local-server state and account operations. The game owns external
 * resources; this APK never copies or imports a resource archive.
 */
public final class LocalServiceActivity extends Activity {

    private static final String CHANNEL_ID = "soultide_local_service";
    private static final int ACCOUNT_REQUEST = 1702;
    private static final int EXPORT_REQUEST = 1703;
    private static final long POLL_INTERVAL_MS = 1000L;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable pollTask = new Runnable() {
        @Override
        public void run() {
            refreshStatus();
            mainHandler.postDelayed(this, POLL_INTERVAL_MS);
        }
    };

    private TextView statusView;
    private TextView detailView;
    private ProgressBar progressBar;
    private Button accountButton;
    private Button exportButton;
    private Button unlockButton;
    private Button startButton;
    private Button stopButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        createNotificationChannel();
        buildUi();
    }

    @Override
    protected void onStart() {
        super.onStart();
        requestNotificationPermissionIfNeeded();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshStatus();
        mainHandler.postDelayed(pollTask, POLL_INTERVAL_MS);
    }

    @Override
    protected void onPause() {
        mainHandler.removeCallbacks(pollTask);
        super.onPause();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == EXPORT_REQUEST) {
            if (resultCode == RESULT_OK && data != null && data.getData() != null) {
                saveExportedAccount(data.getData());
            } else {
                showDetail("已取消账号导出", "当前账号仍保留在本地。");
            }
            return;
        }
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            if (requestCode == ACCOUNT_REQUEST) {
                showDetail("已跳过账号", "未选择账号文件，当前数据库未改动。");
            }
            return;
        }
        copySelectedFile(requestCode, data.getData());
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[] {android.Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(dp(24), dp(24), dp(24), dp(24));

        TextView title = new TextView(this);
        title.setText(R.string.app_name);
        title.setTextSize(20);
        title.setGravity(Gravity.CENTER);
        root.addView(title, layoutParams());

        statusView = new TextView(this);
        statusView.setTextSize(22);
        statusView.setGravity(Gravity.CENTER);
        root.addView(statusView, layoutParams());

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        root.addView(progressBar, layoutParams());

        detailView = new TextView(this);
        detailView.setTextSize(13);
        detailView.setGravity(Gravity.CENTER);
        detailView.setTextColor(Color.DKGRAY);
        root.addView(detailView, layoutParams());

        accountButton = new Button(this);
        accountButton.setText("选择账号文件");
        accountButton.setOnClickListener(v -> startActivityForResult(
                ImportController.openDocument("application/octet-stream"), ACCOUNT_REQUEST));
        root.addView(accountButton, layoutParams());

        exportButton = new Button(this);
        exportButton.setText("导出当前账号");
        exportButton.setOnClickListener(v -> beginAccountExport());
        root.addView(exportButton, layoutParams());

        unlockButton = new Button(this);
        unlockButton.setText("当前账号：全解锁皮肤和物品");
        unlockButton.setOnClickListener(v -> beginEntitlementUnlock());
        root.addView(unlockButton, layoutParams());

        startButton = new Button(this);
        startButton.setText("启动本地服务");
        startButton.setOnClickListener(v -> {
            Intent intent = new Intent(this, LocalBackendService.class);
            startForegroundService(intent);
            refreshStatus();
        });
        root.addView(startButton, layoutParams());

        stopButton = new Button(this);
        stopButton.setText("停止本地服务");
        stopButton.setOnClickListener(v -> {
            stopService(new Intent(this, LocalBackendService.class));
            refreshStatus();
        });
        root.addView(stopButton, layoutParams());

        setContentView(root);
    }

    private LinearLayout.LayoutParams layoutParams() {
        LinearLayout.LayoutParams params =
                new LinearLayout.LayoutParams(
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        LinearLayout.LayoutParams.WRAP_CONTENT);
        params.setMargins(0, dp(8), 0, dp(8));
        return params;
    }

    private void copySelectedFile(final int requestCode, final Uri uri) {
        if (requestCode != ACCOUNT_REQUEST) return;
        final String name = ImportController.ACCOUNT_PACK_NAME;
        showDetail("正在复制账号备份", "正在准备账号数据，请稍候。");
        ImportController.copySelection(this, uri, name, new ImportController.CopyListener() {
            @Override
            public void onProgress(long copied, long total) {
                String detail = total > 0L
                        ? "已复制 " + ImportController.formatBytes(copied)
                        + " / " + ImportController.formatBytes(total)
                        : "已复制 " + ImportController.formatBytes(copied);
                showDetail("正在复制账号备份", detail);
            }

            @Override
            public void onDone(File destination) {
                ImportController.trace(LocalServiceActivity.this,
                        "copied " + name + " -> " + destination.getAbsolutePath());
                mainHandler.post(() -> {
                    showDetail("账号备份已准备",
                            "已复制到本地目录。启动本地服务后会恢复该账号。");
                    refreshStatus();
                });
            }

            @Override
            public void onError(Throwable error) {
                showDetail("复制失败", error.getMessage() == null ? "未知错误" : error.getMessage());
            }
        });
    }

    private void beginAccountExport() {
        try {
            ImportController.requestAccountExport(this);
            showDetail("正在导出当前账号", "正在等待本地服务生成账号文件。");
            waitForExport();
        } catch (Throwable error) {
            showDetail("账号导出失败", error.getMessage() == null ? "未知错误" : error.getMessage());
        }
    }

    private void beginEntitlementUnlock() {
        try {
            ImportController.requestEntitlementUnlock(this);
            showDetail("正在更新当前账号", "将解锁全部皮肤和物品，本地服务会短暂重启。");
            refreshStatus();
        } catch (Throwable error) {
            showDetail("账号全解锁失败", error.getMessage() == null ? "未知错误" : error.getMessage());
        }
    }

    private void waitForExport() {
        final Handler handler = new Handler(Looper.getMainLooper());
        final long deadline = System.currentTimeMillis() + 120000L;
        final Runnable watcher = new Runnable() {
            @Override
            public void run() {
                ImportController.Progress progress = ImportController.readProgress(LocalServiceActivity.this);
                if (progress != null && "export-complete".equals(progress.stage)) {
                    Intent intent = ImportController.createDocument(
                            "application/json", ImportController.EXPORT_NAME);
                    startActivityForResult(intent, EXPORT_REQUEST);
                    return;
                }
                if (progress != null && "failed".equals(progress.stage)) {
                    showDetail("账号导出失败",
                            progress.error.isEmpty() ? progress.message : progress.error);
                    return;
                }
                if (System.currentTimeMillis() > deadline) {
                    showDetail("账号导出超时", "请检查本地服务是否仍在运行。");
                    return;
                }
                handler.postDelayed(this, 500L);
            }
        };
        handler.postDelayed(watcher, 500L);
    }

    private void saveExportedAccount(final Uri destination) {
        showDetail("正在保存账号备份", "正在写入你选择的位置。");
        ImportController.saveExportToUri(this, destination, () -> {
            showDetail("账号备份已保存",
                    "文件可用于以后恢复账号。");
            Toast.makeText(LocalServiceActivity.this, "账号备份已保存", Toast.LENGTH_LONG).show();
        }, error -> showDetail("保存失败",
                error.getMessage() == null ? "未知错误" : error.getMessage()));
    }

    private ServiceStatus currentStatus = ServiceStatus.NOT_STARTED;
    private String currentDetail = "";
    private int currentPercent = -1;

    private void refreshStatus() {
        ImportController.Progress progress = ImportController.readProgress(this);
        boolean serviceRunning = LocalBackendService.isRunning();
        ServiceStatus status;
        String detail;
        if (ImportController.isFailed(progress)) {
            status = ServiceStatus.FAILED;
            detail = progress.error.isEmpty() ? progress.message : progress.error;
        } else if (ImportController.isImportActive(progress)
                && !("starting".equals(progress.stage)
                && serviceRunning && RuntimeController.isBackendHealthy())) {
            status = ServiceStatus.BUSY;
            detail = progress.message;
        } else if (serviceRunning && RuntimeController.isBackendHealthy()) {
            status = ServiceStatus.READY;
            String endpoints = "本地服务运行中（SDK " + RuntimeController.SDK_PORT
                    + " / HTTP " + RuntimeController.HTTP_PORT
                    + " / TCP " + RuntimeController.TCP_PORT
                    + "）。游戏外置资源请手动放入游戏目录。";
            detail = progress != null && "unlock-complete".equals(progress.stage)
                    ? progress.message + "\n" + endpoints : endpoints;
        } else if (serviceRunning) {
            status = ServiceStatus.BUSY;
            detail = progress != null && !progress.message.isEmpty()
                    ? progress.message : "正在启动本地服务，请稍候。";
        } else {
            status = ServiceStatus.NOT_STARTED;
            detail = "服务未启动。游戏外置资源由用户手动移动，服务 APK 不导入资源包。";
        }

        int percent = -1;
        if (status != currentStatus || !detail.equals(currentDetail) || percent != currentPercent) {
            render(status, detail, percent);
        }

        boolean accountEnabled = !serviceRunning && ServiceStatus.BUSY != status;
        if (accountButton.isEnabled() != accountEnabled) {
            accountButton.setEnabled(accountEnabled);
        }
        if (exportButton.isEnabled() != (ServiceStatus.READY == status && serviceRunning)) {
            exportButton.setEnabled(ServiceStatus.READY == status && serviceRunning);
        }
        if (unlockButton.isEnabled() != (ServiceStatus.READY == status && serviceRunning)) {
            unlockButton.setEnabled(ServiceStatus.READY == status && serviceRunning);
        }
        if (startButton.isEnabled() != !serviceRunning) {
            startButton.setEnabled(!serviceRunning);
        }
        if (stopButton.isEnabled() != serviceRunning) {
            stopButton.setEnabled(serviceRunning);
        }
    }

    private void render(ServiceStatus status, String detail, int percent) {
        currentStatus = status;
        currentDetail = detail;
        currentPercent = percent;
        statusView.setText(status.label());
        switch (status) {
            case READY:
                statusView.setTextColor(Color.parseColor("#1B5E20"));
                break;
            case BUSY:
                statusView.setTextColor(Color.parseColor("#E65100"));
                break;
            case FAILED:
                statusView.setTextColor(Color.RED);
                break;
            default:
                statusView.setTextColor(Color.DKGRAY);
                break;
        }
        detailView.setText(detail);
        progressBar.setVisibility(ServiceStatus.BUSY == status ? android.view.View.VISIBLE : android.view.View.INVISIBLE);
        if (ServiceStatus.BUSY == status) {
            if (percent >= 0) {
                progressBar.setIndeterminate(false);
                progressBar.setProgress(percent);
            } else {
                progressBar.setIndeterminate(true);
            }
        } else {
            progressBar.setIndeterminate(false);
        }
    }

    private void showDetail(final String title, final String detail) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            statusView.setText(title);
            detailView.setText(detail);
        } else {
            mainHandler.post(() -> showDetail(title, detail));
        }
    }

    private void createNotificationChannel() {
        NotificationManager manager =
                (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "本地服务",
                NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("本地服务运行状态");
        manager.createNotificationChannel(channel);
    }

    static String channelId() {
        return CHANNEL_ID;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
