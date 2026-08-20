package com.feteeve.iptimeprobe;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.CookieHandler;
import java.net.CookieManager;
import java.net.CookiePolicy;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class IptimeProbeClient {
    enum Mode { AUTO, ORIGINAL, BETA, LEGACY }

    interface Logger { void log(String message); }

    static final class ProbeException extends Exception {
        ProbeException(String message) { super(message); }
        ProbeException(String message, Throwable cause) { super(message, cause); }
    }

    private static final Pattern MAC_PATTERN = Pattern.compile(
            "([0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5})");
    private static final Pattern IP_PATTERN = Pattern.compile("(\\d{1,3}(?:\\.\\d{1,3}){3})");
    private static final Pattern RSSI_PATTERN = Pattern.compile("(-?\\d+)\\s*dBm", Pattern.CASE_INSENSITIVE);
    private static final Pattern ROW_PATTERN = Pattern.compile("<tr[^>]*>.*?</tr>", Pattern.CASE_INSENSITIVE | Pattern.DOTALL);
    private static final Pattern CELL_PATTERN = Pattern.compile("<td[^>]*>(.*?)</td>", Pattern.CASE_INSENSITIVE | Pattern.DOTALL);
    private static final Pattern TAG_PATTERN = Pattern.compile("<[^>]+>");

    private final String baseUrl;
    private final String username;
    private final String password;
    private final Mode requestedMode;
    private final Logger logger;
    private final CookieManager cookieManager = new CookieManager(null, CookiePolicy.ACCEPT_ALL);

    IptimeProbeClient(String host, String username, String password, Mode mode, Logger logger) {
        String normalized = host.trim();
        if (!normalized.startsWith("http://") && !normalized.startsWith("https://")) {
            normalized = "http://" + normalized;
        }
        while (normalized.endsWith("/")) {
            normalized = normalized.substring(0, normalized.length() - 1);
        }
        this.baseUrl = normalized;
        this.username = username;
        this.password = password;
        this.requestedMode = mode;
        this.logger = logger;
        CookieHandler.setDefault(cookieManager);
    }

    List<ConnectedDevice> run() throws ProbeException {
        logger.log("공유기 접속 확인: " + baseUrl);
        Mode mode = requestedMode;
        if (mode == Mode.AUTO) {
            mode = supportsBetaUi() ? Mode.BETA : Mode.ORIGINAL;
        }
        logger.log("사용 방식: " + modeLabel(mode));

        List<ConnectedDevice> devices;
        if (mode == Mode.BETA) {
            loginBeta();
            devices = fetchBetaDevices();
        } else if (mode == Mode.LEGACY) {
            loginLegacy();
            devices = fetchLegacyDevices();
        } else {
            loginOriginal();
            devices = fetchOriginalDevices();
        }
        logger.log("조회 완료: 현재 접속 기기 " + devices.size() + "대");
        return devices;
    }

    private String modeLabel(Mode mode) {
        switch (mode) {
            case BETA: return "신형(Beta API)";
            case LEGACY: return "구형 로그인 핸들러";
            case ORIGINAL: return "수정 전 방식(timepro POST)";
            default: return "자동";
        }
    }

    private boolean supportsBetaUi() {
        String[] paths = {"/ui/flutter_bootstrap.js", "/ui/"};
        for (String path : paths) {
            try {
                String body = request("GET", path, null, null);
                if (body.contains("/cgi/service.cgi") || body.toLowerCase(Locale.ROOT).contains("flutter")) {
                    return true;
                }
            } catch (ProbeException error) {
                logger.log("신형 UI 확인 실패(" + path + "): " + error.getMessage());
            }
        }
        return false;
    }

    private void loginOriginal() throws ProbeException {
        Map<String, String> form = new LinkedHashMap<>();
        form.put("tmenu", "iframe");
        form.put("smenu", "login_proc");
        form.put("username", username);
        form.put("passwd", password);
        String body = request("POST", "/sess-bin/timepro.cgi", form, null);
        ensureNotLoginPage(body, true);
        logger.log("수정 전 방식 로그인 응답 성공 (세션 쿠키: "
                + (hasSessionCookie() ? "있음" : "없음") + ")");
    }

    private void loginLegacy() throws ProbeException {
        Map<String, String> form = new LinkedHashMap<>();
        form.put("username", username);
        form.put("passwd", password);
        String body = request("POST", "/sess-bin/login_handler.cgi", form, null);
        ensureNotLoginPage(body, false);
        if (!hasSessionCookie()) {
            Matcher session = Pattern.compile("\\b([A-Za-z0-9]{16})\\b").matcher(body);
            if (session.find()) {
                try {
                    cookieManager.getCookieStore().add(
                            URI.create(baseUrl),
                            new java.net.HttpCookie("efm_session_id", session.group(1)));
                } catch (IllegalArgumentException error) {
                    throw new ProbeException("세션 쿠키 저장 실패", error);
                }
            }
        }
        if (!hasSessionCookie()) {
            throw new ProbeException("로그인 응답에서 세션 값을 찾지 못했습니다");
        }
        logger.log("구형 UI 로그인 성공");
    }

    private void loginBeta() throws ProbeException {
        JSONObject params = new JSONObject();
        try {
            params.put("id", username);
            params.put("pw", password);
        } catch (JSONException impossible) {
            throw new ProbeException("로그인 요청 생성 실패", impossible);
        }
        JSONObject response = requestJson("session/login", params);
        if (response.optBoolean("result", false)) {
            logger.log("신형 UI 로그인 성공");
            return;
        }
        int code = response.optJSONObject("error") != null
                ? response.optJSONObject("error").optInt("code", 0) : 0;
        if (code == -31997) throw new ProbeException("CAPTCHA 인증이 활성화되어 있습니다");
        if (code == -31996) throw new ProbeException("관리자 아이디 또는 비밀번호가 틀렸습니다");
        throw new ProbeException("신형 UI 로그인 실패: error code " + code);
    }

    private List<ConnectedDevice> fetchOriginalDevices() throws ProbeException {
        LinkedHashMap<String, ConnectedDevice> devices = new LinkedHashMap<>();
        String[][] menus = {
                {"wireless_association", "2.4GHz"},
                {"wireless_association_5g", "5GHz"},
                {"wireless_association_6g", "6GHz"},
                {"lan_pcinfo_status", "LAN/unknown"}
        };
        for (String[] menu : menus) {
            Map<String, String> form = new LinkedHashMap<>();
            form.put("tmenu", "iframe");
            form.put("smenu", menu[0]);
            String body = request("POST", "/sess-bin/timepro.cgi", form, null);
            ensureNotLoginPage(body, false);
            List<ConnectedDevice> parsed = parseHtmlDevices(body, menu[1]);
            logger.log("원래 메뉴 " + menu[0] + ": " + parsed.size() + "대 파싱");
            for (ConnectedDevice device : parsed) devices.putIfAbsent(device.mac, device);
        }
        return new ArrayList<>(devices.values());
    }

    private List<ConnectedDevice> fetchLegacyDevices() throws ProbeException {
        LinkedHashMap<String, ConnectedDevice> devices = new LinkedHashMap<>();
        String[][] bands = {{"0", "2.4GHz"}, {"65536", "5GHz"}};
        for (String[] band : bands) {
            String path = "/sess-bin/timepro.cgi?tmenu=iframe&smenu=macauth_pcinfo_status&bssidx=" + band[0];
            String body = request("GET", path, null, null);
            ensureNotLoginPage(body, false);
            for (ConnectedDevice device : parseHtmlDevices(body, band[1])) {
                devices.put(device.mac, device);
            }
        }
        String body = request("GET", "/sess-bin/timepro.cgi?tmenu=iframe&smenu=lan_pcinfo_status", null, null);
        ensureNotLoginPage(body, false);
        for (ConnectedDevice device : parseHtmlDevices(body, "LAN/unknown")) {
            devices.putIfAbsent(device.mac, device);
        }
        return new ArrayList<>(devices.values());
    }

    private List<ConnectedDevice> fetchBetaDevices() throws ProbeException {
        JSONObject response = requestJson("network/interface/lan/stations", null);
        JSONArray result = response.optJSONArray("result");
        if (result == null) {
            JSONObject error = response.optJSONObject("error");
            throw new ProbeException("접속자 조회 실패: error code "
                    + (error != null ? error.optInt("code", 0) : 0));
        }
        List<ConnectedDevice> devices = new ArrayList<>();
        for (int i = 0; i < result.length(); i++) {
            JSONObject item = result.optJSONObject(i);
            if (item == null) continue;
            JSONObject connection = item.optJSONObject("connection");
            if (connection == null) connection = new JSONObject();
            String type = connection.optString("type", "unknown");
            JSONObject details = connection.optJSONObject(type);
            if (details == null) details = new JSONObject();
            JSONObject info = item.optJSONObject("info");
            if (info == null) info = new JSONObject();
            String bss = details.optString("bss", type);
            String label = bss.equals("2g.1") ? "2.4GHz"
                    : bss.equals("5g.1") ? "5GHz"
                    : bss.equals("6g.1") ? "6GHz" : bss;
            String mac = normalizeMac(item.optString("mac", ""));
            if (mac.isEmpty()) continue;
            Integer rssi = details.has("rssi") ? details.optInt("rssi") : null;
            devices.add(new ConnectedDevice(
                    mac,
                    info.optString("ip", ""),
                    info.optString("name", info.optString("hostname", "")),
                    label,
                    rssi));
        }
        return devices;
    }

    private JSONObject requestJson(String method, JSONObject params) throws ProbeException {
        JSONObject payload = new JSONObject();
        try {
            payload.put("method", method);
            if (params != null) payload.put("params", params);
            return new JSONObject(request("POST", "/cgi/service.cgi", null, payload.toString()));
        } catch (JSONException error) {
            throw new ProbeException(method + ": JSON 응답 해석 실패", error);
        }
    }

    private String request(String method, String path, Map<String, String> form, String json)
            throws ProbeException {
        HttpURLConnection connection = null;
        try {
            URL url = new URL(baseUrl + path);
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(10_000);
            connection.setInstanceFollowRedirects(true);
            connection.setRequestMethod(method);
            connection.setRequestProperty("User-Agent", "Mozilla/5.0 (Android ipTIME Probe)");
            connection.setRequestProperty("Accept-Language", "ko-KR,ko;q=0.9,en;q=0.8");

            byte[] data = null;
            if (form != null) {
                StringBuilder encoded = new StringBuilder();
                for (Map.Entry<String, String> entry : form.entrySet()) {
                    if (encoded.length() > 0) encoded.append('&');
                    encoded.append(URLEncoder.encode(entry.getKey(), "UTF-8"));
                    encoded.append('=');
                    encoded.append(URLEncoder.encode(entry.getValue(), "UTF-8"));
                }
                data = encoded.toString().getBytes(StandardCharsets.UTF_8);
                connection.setRequestProperty("Content-Type", "application/x-www-form-urlencoded");
            } else if (json != null) {
                data = json.getBytes(StandardCharsets.UTF_8);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            }
            if (data != null) {
                connection.setDoOutput(true);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(data);
                }
            }

            int status = connection.getResponseCode();
            InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
            String body = readText(stream);
            logger.log("HTTP " + status + " " + method + " " + path.split("\\?")[0]
                    + " (" + body.getBytes(StandardCharsets.UTF_8).length + " bytes)");
            if (status >= 400) throw new ProbeException("HTTP " + status + ": " + path);
            return body;
        } catch (IOException error) {
            throw new ProbeException("연결 실패: " + error.getMessage(), error);
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private String readText(InputStream stream) throws IOException {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) result.append(line).append('\n');
        }
        return result.toString();
    }

    private boolean hasSessionCookie() {
        return cookieManager.getCookieStore().getCookies().stream()
                .anyMatch(cookie -> cookie.getName().equals("efm_session_id"));
    }

    private void ensureNotLoginPage(String body, boolean originalLogin) throws ProbeException {
        String lower = body.toLowerCase(Locale.ROOT);
        if (lower.contains("captcha") && !lower.contains("captcha_on")) {
            throw new ProbeException("CAPTCHA 인증이 활성화되어 있습니다");
        }
        if (body.contains("login_session.cgi?noauto=1")
                || body.contains("name=\"passwd\"")
                || body.contains("efm_pwchk")
                || (originalLogin && body.contains("name=\"username\""))) {
            throw new ProbeException("관리자 아이디 또는 비밀번호가 틀렸습니다");
        }
    }

    private List<ConnectedDevice> parseHtmlDevices(String document, String connection) {
        List<ConnectedDevice> devices = new ArrayList<>();
        Matcher rows = ROW_PATTERN.matcher(document);
        while (rows.find()) {
            String row = rows.group();
            Matcher macMatcher = MAC_PATTERN.matcher(row);
            if (!macMatcher.find()) continue;
            List<String> cells = new ArrayList<>();
            Matcher cellMatcher = CELL_PATTERN.matcher(row);
            while (cellMatcher.find()) {
                cells.add(decodeHtml(TAG_PATTERN.matcher(cellMatcher.group(1)).replaceAll("")));
            }
            String joined = String.join(" ", cells);
            Matcher ipMatcher = IP_PATTERN.matcher(joined);
            Matcher rssiMatcher = RSSI_PATTERN.matcher(joined);
            String hostname = "";
            for (String cell : cells) {
                if (!MAC_PATTERN.matcher(cell).find()
                        && !IP_PATTERN.matcher(cell).find()
                        && !RSSI_PATTERN.matcher(cell).find()
                        && !cell.trim().isEmpty()) {
                    hostname = cell.trim();
                    break;
                }
            }
            devices.add(new ConnectedDevice(
                    normalizeMac(macMatcher.group(1)),
                    ipMatcher.find() ? ipMatcher.group(1) : "",
                    hostname,
                    connection,
                    rssiMatcher.find() ? Integer.parseInt(rssiMatcher.group(1)) : null));
        }
        return devices;
    }

    private String decodeHtml(String value) {
        return value.replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replaceAll("\\s+", " ")
                .trim();
    }

    private String normalizeMac(String value) {
        Matcher matcher = MAC_PATTERN.matcher(value);
        return matcher.find() ? matcher.group(1).toUpperCase(Locale.ROOT).replace('-', ':') : "";
    }
}
