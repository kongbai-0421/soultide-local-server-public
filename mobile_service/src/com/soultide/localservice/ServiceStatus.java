package com.soultide.localservice;

/**
 * Stable service states shown by the UI.
 */
public enum ServiceStatus {
    NOT_STARTED("服务未启动"),
    BUSY("正在处理账号操作"),
    READY("本地服务已就绪"),
    FAILED("失败");

    private final String label;

    ServiceStatus(String label) {
        this.label = label;
    }

    public String label() {
        return label;
    }
}
