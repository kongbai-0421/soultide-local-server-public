package org.kivy.android;

/**
 * Minimal JNI binding for the p4a python-service native bridge
 * (provided by libp4a_.so inside the bundled runtime).
 *
 * The class name, method name and signature must match the native export
 * Java_org_kivy_android_PythonService_nativeStart exactly; see
 * mobile_service/CARD3.md for the parameter contract (verified against the
 * classes6.dex of the baseline story26 APK).
 */
public final class PythonService {

    /**
     * Starts the embedded Python interpreter and the service entry point.
     * Blocks until the interpreter shuts down; call from a dedicated thread.
     *
     * @param apkDir       runtime root extracted to app-private files
     * @param bootstrap    runtime server directory
     * @param bootstrapName service entry script (mobile_entry.py)
     * @param pythonPath   service name token (unused by this runtime)
     * @param pythonHome   _python_bundle directory (PYTHONHOME)
     * @param serviceName  PYTHONPATH (stdlib.zip:modules)
     * @param argv1        extra argument (empty)
     */
    public static native void nativeStart(
            String apkDir,
            String bootstrap,
            String bootstrapName,
            String pythonPath,
            String pythonHome,
            String serviceName,
            String argv1);

    private PythonService() {}
}
