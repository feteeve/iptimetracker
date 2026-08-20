package com.feteeve.iptimeprobe;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private EditText hostInput;
    private EditText usernameInput;
    private EditText passwordInput;
    private Spinner modeInput;
    private Button runButton;
    private TextView output;
    private ScrollView outputScroll;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
    }

    private void buildUi() {
        int padding = dp(16);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(padding, padding, padding, padding);
        root.setBackgroundColor(Color.rgb(245, 247, 250));

        TextView title = new TextView(this);
        title.setText("ipTIME 접속 테스트");
        title.setTextSize(24);
        title.setTextColor(Color.rgb(25, 45, 70));
        title.setPadding(0, 0, 0, dp(12));
        root.addView(title);

        SharedPreferences preferences = getSharedPreferences("probe", MODE_PRIVATE);
        hostInput = addInput(root, "공유기 주소", preferences.getString("host", "192.168.0.1"), false);
        usernameInput = addInput(root, "관리자 아이디", preferences.getString("username", "admin"), false);
        passwordInput = addInput(root, "관리자 비밀번호", "", true);

        TextView modeLabel = new TextView(this);
        modeLabel.setText("접속 방식");
        modeLabel.setTextColor(Color.DKGRAY);
        root.addView(modeLabel);
        modeInput = new Spinner(this);
        String[] modes = {"수정 전 방식 (권장)", "자동 감지", "신형 Beta API", "구형 로그인 핸들러"};
        modeInput.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, modes));
        root.addView(modeInput, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));

        runButton = new Button(this);
        runButton.setText("로그인 및 전체 접속자 조회");
        LinearLayout.LayoutParams buttonParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        buttonParams.topMargin = dp(12);
        root.addView(runButton, buttonParams);
        runButton.setOnClickListener(view -> runProbe());

        output = new TextView(this);
        output.setTextSize(13);
        output.setTextColor(Color.rgb(20, 30, 40));
        output.setTextIsSelectable(true);
        output.setPadding(dp(12), dp(12), dp(12), dp(12));
        output.setBackgroundColor(Color.WHITE);
        outputScroll = new ScrollView(this);
        outputScroll.addView(output);
        LinearLayout.LayoutParams scrollParams = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, 0, 1);
        scrollParams.topMargin = dp(12);
        root.addView(outputScroll, scrollParams);
        setContentView(root);
    }

    private EditText addInput(LinearLayout root, String hint, String value, boolean password) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setText(value);
        input.setSingleLine(true);
        if (password) {
            input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        }
        root.addView(input, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        return input;
    }

    private void runProbe() {
        String host = hostInput.getText().toString().trim();
        String username = usernameInput.getText().toString();
        String password = passwordInput.getText().toString();
        IptimeProbeClient.Mode mode = selectedMode();
        getSharedPreferences("probe", MODE_PRIVATE).edit()
                .putString("host", host)
                .putString("username", username)
                .apply();

        output.setText("");
        runButton.setEnabled(false);
        append("테스트 시작\n");
        executor.execute(() -> {
            IptimeProbeClient client = new IptimeProbeClient(
                    host, username, password, mode, message -> runOnUiThread(() -> append(message + "\n")));
            try {
                List<ConnectedDevice> devices = client.run();
                runOnUiThread(() -> showDevices(devices));
            } catch (Exception error) {
                runOnUiThread(() -> {
                    append("\n실패: " + error.getMessage() + "\n");
                    runButton.setEnabled(true);
                });
            }
        });
    }

    private IptimeProbeClient.Mode selectedMode() {
        switch (modeInput.getSelectedItemPosition()) {
            case 1: return IptimeProbeClient.Mode.AUTO;
            case 2: return IptimeProbeClient.Mode.BETA;
            case 3: return IptimeProbeClient.Mode.LEGACY;
            default: return IptimeProbeClient.Mode.ORIGINAL;
        }
    }

    private void showDevices(List<ConnectedDevice> devices) {
        append("\nMAC / IP / 연결 / RSSI / 이름\n");
        append("────────────────────────────\n");
        if (devices.isEmpty()) {
            append("현재 접속 중인 기기가 없습니다.\n");
        } else {
            for (ConnectedDevice device : devices) {
                String rssi = device.rssi == null ? "-" : device.rssi.toString();
                append(String.format(Locale.KOREA, "%s\n%s · %s · %s · %s\n\n",
                        device.mac, device.ip, device.connection, rssi, device.hostname));
            }
        }
        runButton.setEnabled(true);
    }

    private void append(String message) {
        output.append(message);
        outputScroll.post(() -> outputScroll.fullScroll(View.FOCUS_DOWN));
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }
}
