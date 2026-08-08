package com.soultide.localservice;

import android.content.Context;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Native loopback HTTP transport. Python remains responsible for game TCP. */
final class LocalHttpServer {
    private static final String TAG = "SoulTideLocal";
    private static final String UID = "local-test-dollmaster";
    private static final String TOKEN = "local_token_15dcba9291fef1f76a3288c01121a1b4";
    private static final String UUID = "95349c9a-4a0d-5e11-aa7f-bd1240cb5bdf";
    private static volatile boolean running;
    private static ServerSocket sdkSocket;
    private static ServerSocket gameSocket;
    private static ExecutorService workers;
    private static File assets;
    private static File server;

    private LocalHttpServer() {}

    static synchronized void start(Context context, File runtime) throws IOException {
        if (running) return;
        assets = new File(new File(context.getFilesDir(), "mobile-data"), "offline_cdn/Android");
        server = new File(runtime, "server");
        workers = Executors.newCachedThreadPool();
        sdkSocket = open(RuntimeController.SDK_PORT);
        try { gameSocket = open(RuntimeController.HTTP_PORT); }
        catch (IOException error) { close(sdkSocket); workers.shutdownNow(); workers = null; throw error; }
        running = true;
        accept(sdkSocket, true, "soultide-sdk-http");
        accept(gameSocket, false, "soultide-game-http");
        RuntimeController.trace(context, "native HTTP listeners started (sdk=8000 http=8081)");
    }

    static synchronized void stop() {
        running = false;
        close(sdkSocket); close(gameSocket); sdkSocket = null; gameSocket = null;
        if (workers != null) workers.shutdownNow();
        workers = null;
    }

    private static ServerSocket open(int port) throws IOException {
        ServerSocket socket = new ServerSocket();
        socket.setReuseAddress(true);
        socket.bind(new java.net.InetSocketAddress(InetAddress.getByName("127.0.0.1"), port));
        return socket;
    }

    private static void accept(final ServerSocket listener, final boolean sdk, String name) {
        Thread thread = new Thread(() -> {
            while (running) try {
                final Socket client = listener.accept();
                ExecutorService executor = workers;
                if (executor != null) executor.execute(() -> handle(client, sdk)); else close(client);
            } catch (IOException error) { if (running) Log.w(TAG, "HTTP accept failed", error); }
        }, name);
        thread.setDaemon(true);
        thread.start();
    }

    private static void handle(Socket client, boolean sdk) {
        try {
            client.setSoTimeout(15000);
            Request request = request(client.getInputStream());
            if (request == null) return;
            if (sdk) sdk(client, request); else game(client, request);
        } catch (Exception error) { Log.w(TAG, "HTTP request failed", error); }
        finally { close(client); }
    }

    private static Request request(InputStream source) throws IOException {
        BufferedInputStream input = new BufferedInputStream(source);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        int state = 0;
        while (bytes.size() < 65536) {
            int value = input.read();
            if (value < 0) return null;
            bytes.write(value);
            state = state == 0 && value == '\r' ? 1 : state == 1 && value == '\n' ? 2
                    : state == 2 && value == '\r' ? 3 : state == 3 && value == '\n' ? 4
                    : value == '\r' ? 1 : 0;
            if (state == 4) break;
        }
        if (state != 4) return null;
        String[] lines = new String(bytes.toByteArray(), StandardCharsets.ISO_8859_1).split("\\r\\n");
        String[] first = lines[0].split(" ");
        if (first.length < 2) return null;
        String range = "";
        for (String line : lines) if (line.toLowerCase(Locale.US).startsWith("range:")) range = line.substring(6).trim();
        String target = first[1]; int query = target.indexOf('?');
        return new Request(first[0].toUpperCase(Locale.US), query < 0 ? target : target.substring(0, query), range);
    }

    private static void sdk(Socket client, Request request) throws IOException {
        String path = request.path;
        if ("/health".equals(path) || "/ping".equals(path)) json(client, request, "{\"code\":0,\"msg\":\"pong\"}");
        else if ("/client/init".equals(path)) json(client, request, "{\"code\":0,\"msg\":\"success\",\"data\":{\"agreements\":{\"list\":[],\"version\":7,\"switch\":true},\"sdk_switch\":{},\"open_url\":{}}}");
        else if ("/client/checkUser".equals(path)) json(client, request, "{\"code\":0,\"msg\":\"success\",\"data\":{\"user_info\":{\"usdk_uid\":\"" + UID + "\",\"usdk_token\":\"" + TOKEN + "\",\"usdk_username\":\"local_player\"}}}");
        else if (path.contains("/iqisdk/agreement/")) send(client, request, 200, "text/html; charset=utf-8", "<html><body>Local offline service</body></html>".getBytes(StandardCharsets.UTF_8), "");
        else json(client, request, "{\"code\":0,\"msg\":\"success\",\"data\":{}}");
    }

    private static void game(Socket client, Request request) throws IOException {
        String path = request.path;
        if ("/health".equals(path) || "/ping".equals(path)) { json(client, request, "{\"code\":0,\"msg\":\"pong\"}"); return; }
        if ("/api/clientInfo/".equals(path)) { json(client, request, "{\"msg\":\"ok\",\"code\":\"0\",\"submitMode\":\"0\"}"); return; }
        if ("/Onigao/Update/version-Android.txt".equals(path)) { send(client, request, 200, "text/plain; charset=utf-8", version().getBytes(StandardCharsets.UTF_8), ""); return; }
        if ("/login/user_login/".equals(path)) {
            json(client, request, "{\"code\":0,\"data\":{\"uid\":\"" + UID + "\",\"lastLoginServerId\":\"1121\",\"accountServerId\":\"2001\",\"districts\":[{\"serverId\":\"1121\",\"areaId\":\"101\",\"areaName\":\"\\u65b0\\u6708\",\"serverName\":\"\\u65b0\\u6708\",\"isRmd\":1,\"state\":1,\"downTimeInfo\":\"local\",\"serverIp\":\"127.0.0.1\",\"port\":51121,\"roleCount\":1}],\"activation\":true,\"uuid\":\"" + UUID + "\",\"serverTime\":" + (System.currentTimeMillis() / 1000L) + "}}"); return;
        }
        if ("/ng/client/system.getSecurityKey".equals(path)) {
            File key = new File(server, "sdk_security_key_response.bin");
            if (key.isFile()) file(client, request, key); else send(client, request, 503, "application/octet-stream", new byte[0], "");
            return;
        }
        File resource = resource(path);
        if (resource != null) { if (resource.isFile()) file(client, request, resource); else status(client, request, 404, "resource not found"); return; }
        if (path.startsWith("/Onigao/Media/")) { File media = safe(path.substring(14)); if (media != null && media.isFile()) file(client, request, media); else status(client, request, 404, "media not found"); return; }
        json(client, request, "{\"code\":0,\"msg\":\"success\",\"data\":{}}");
    }

    private static File resource(String path) {
        String prefix = "/Onigao/Update/resources/";
        if (!path.startsWith(prefix)) return null;
        int marker = path.indexOf("/Android/", prefix.length());
        if (marker < 0) return new File("");
        String relative = path.substring(marker + 9);
        return "version.json".equalsIgnoreCase(relative) ? versionManifest() : safe(relative);
    }

    /**
     * The full Unity resource pack belongs to the game app on Android 13 and
     * is not readable by this service app. Keep its small version manifest in
     * the service runtime so the first update check can succeed independently.
     */
    private static File versionManifest() {
        File imported = new File(assets, "version-remote.json");
        if (imported.isFile()) return imported;
        return new File(server, "version-local-default.json");
    }

    private static File safe(String raw) {
        try {
            String relative = URLDecoder.decode(raw, "UTF-8").replace('\\', '/');
            if (relative.contains("..")) return null;
            File file = new File(assets, relative).getCanonicalFile();
            return file.getPath().startsWith(assets.getCanonicalPath() + File.separator) ? file : null;
        } catch (Exception error) { return null; }
    }

    private static String version() {
        String game = "0.49.10"; long resource = 0; File manifest = versionManifest();
        try {
            String text = new String(read(manifest), StandardCharsets.UTF_8);
            Matcher g = Pattern.compile("\\\"ApplicableGameVersion\\\"\\s*:\\s*\\\"([^\\\"]+)").matcher(text);
            Matcher r = Pattern.compile("\\\"InternalResourceVersion\\\"\\s*:\\s*(\\d+)").matcher(text);
            if (g.find()) game = g.group(1); if (r.find()) resource = Long.parseLong(r.group(1));
        } catch (Exception ignored) {}
        return "{\"LatestGameVersion\":\"" + game + "\",\"InternalResourceVersion\":" + resource + ",\"VersionListLength\":" + (manifest.isFile() ? manifest.length() : 0) + ",\"UpdateMode\":\"local\"}";
    }

    private static void json(Socket client, Request request, String body) throws IOException { send(client, request, 200, "application/json; charset=utf-8", body.getBytes(StandardCharsets.UTF_8), ""); }
    private static void status(Socket client, Request request, int code, String message) throws IOException { send(client, request, code, "application/json; charset=utf-8", ("{\"code\":" + code + ",\"msg\":\"" + message + "\"}").getBytes(StandardCharsets.UTF_8), ""); }

    private static void file(Socket client, Request request, File file) throws IOException {
        long length = file.length(), start = 0, end = length - 1; boolean partial = false;
        if (request.range.startsWith("bytes=")) try {
            String[] values = request.range.substring(6).split("-", 2);
            start = values[0].isEmpty() ? 0 : Long.parseLong(values[0]);
            if (values.length > 1 && !values[1].isEmpty()) end = Math.min(end, Long.parseLong(values[1]));
            partial = start >= 0 && start <= end;
        } catch (NumberFormatException ignored) {}
        if (request.range.length() > 0 && !partial) { send(client, request, 416, mime(file), new byte[0], "Content-Range: bytes */" + length + "\r\n"); return; }
        long count = length == 0 ? 0 : end - start + 1;
        OutputStream output = client.getOutputStream();
        head(output, partial ? 206 : 200, mime(file), count, "Accept-Ranges: bytes\r\n" + (partial ? "Content-Range: bytes " + start + "-" + end + "/" + length + "\r\n" : ""));
        if ("HEAD".equals(request.method) || count == 0) return;
        try (FileInputStream input = new FileInputStream(file)) { while (start > 0) start -= input.skip(start); copy(input, output, count); }
    }

    private static void send(Socket client, Request request, int code, String type, byte[] body, String extra) throws IOException {
        OutputStream output = client.getOutputStream(); head(output, code, type, body.length, extra); if (!"HEAD".equals(request.method) && body.length > 0) output.write(body); output.flush();
    }
    private static void head(OutputStream output, int code, String type, long length, String extra) throws IOException {
        String reason = code == 200 ? "OK" : code == 206 ? "Partial Content" : code == 404 ? "Not Found" : code == 416 ? "Range Not Satisfiable" : code == 503 ? "Service Unavailable" : "Error";
        output.write(("HTTP/1.1 " + code + " " + reason + "\r\nContent-Type: " + type + "\r\nContent-Length: " + length + "\r\nConnection: close\r\nAccess-Control-Allow-Origin: *\r\n" + extra + "\r\n").getBytes(StandardCharsets.ISO_8859_1));
    }
    private static byte[] read(File file) throws IOException { try (FileInputStream input = new FileInputStream(file); ByteArrayOutputStream output = new ByteArrayOutputStream()) { copy(input, output, -1); return output.toByteArray(); } }
    private static void copy(InputStream input, OutputStream output, long count) throws IOException { byte[] buffer = new byte[65536]; while (count != 0) { int need = count < 0 ? buffer.length : (int)Math.min(buffer.length, count); int got = input.read(buffer, 0, need); if (got < 0) break; output.write(buffer, 0, got); if (count > 0) count -= got; } }
    private static String mime(File file) { String name = file.getName().toLowerCase(Locale.US); if (name.endsWith(".json") || name.endsWith(".txt") || name.endsWith(".lua")) return "application/json; charset=utf-8"; if (name.endsWith(".png")) return "image/png"; if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg"; if (name.endsWith(".mp3")) return "audio/mpeg"; if (name.endsWith(".ogg")) return "audio/ogg"; if (name.endsWith(".mp4")) return "video/mp4"; return "application/octet-stream"; }
    private static void close(java.io.Closeable value) { if (value != null) try { value.close(); } catch (IOException ignored) {} }
    private static final class Request { final String method, path, range; Request(String method, String path, String range) { this.method = method; this.path = path; this.range = range; } }
}
