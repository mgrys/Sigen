package pl.mgrys.sigenpv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Sigen PV Monitor / Sigen Dashboard na telefonie: okno z dashboardem dzialajacym na komputerze w sieci domowej.
 * Adres serwera (np. http://192.168.0.50:8000) podaje sie przy pierwszym uruchomieniu; zmiana: przycisk
 * "Adres serwera" w stopce dashboardu albo okno bledu polaczenia.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "sigen", KEY_URL = "url";
    private WebView web;
    private SharedPreferences prefs;
    private AlertDialog dialog;
    private boolean errorShown;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        web = new WebView(this);
        web.setLayoutParams(new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // dashboard pamieta ukryte serie wykresow itp. (localStorage)
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setUserAgentString(s.getUserAgentString() + " SigenApp/" + BuildConfigCompat.versionName(this));

        CookieManager.getInstance().setAcceptCookie(true);   // sesja po wpisaniu PIN-u
        web.addJavascriptInterface(new Bridge(), "SigenApp");
        web.setWebChromeClient(new WebChromeClient());       // okna potwierdzen (confirm/alert) ze strony
        web.setWebViewClient(new Client());

        if (state != null && web.restoreState(state) != null) {
            return;
        }
        String url = prefs.getString(KEY_URL, null);
        if (url == null) {
            askAddress(false);
        } else {
            web.loadUrl(url);
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
        web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    /** Normalizacja adresu: "192.168.0.50" -> "http://192.168.0.50:8000/". */
    static String normalize(String in) {
        String u = in == null ? "" : in.trim();
        if (u.isEmpty()) return null;
        if (!u.matches("(?i)^https?://.*")) u = "http://" + u;
        Uri p = Uri.parse(u);
        if (p.getHost() == null || p.getHost().isEmpty()) return null;
        if (p.getPort() == -1 && "http".equalsIgnoreCase(p.getScheme())) {
            u = p.getScheme() + "://" + p.getHost() + ":8000" + (p.getPath() == null ? "" : p.getPath());
        }
        String path = Uri.parse(u).getPath();
        if (path == null || path.isEmpty()) u += "/";
        return u;
    }

    private void askAddress(boolean cancelable) {
        if (dialog != null && dialog.isShowing()) dialog.dismiss();
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(pad, pad / 2, pad, 0);
        TextView info = new TextView(this);
        info.setText(R.string.address_info);
        EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        in.setSingleLine(true);
        in.setText(prefs.getString(KEY_URL, "http://192.168.0."));
        in.setSelection(in.getText().length());
        box.addView(info);
        box.addView(in);

        AlertDialog.Builder b = new AlertDialog.Builder(this)
                .setTitle(R.string.address_title)
                .setView(box)
                .setCancelable(cancelable)
                .setPositiveButton(R.string.connect, null);
        if (cancelable) b.setNegativeButton(R.string.cancel, null);
        else b.setNegativeButton(R.string.close_app, (d, w) -> finish());
        dialog = b.create();
        dialog.setOnShowListener(d -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String url = normalize(in.getText().toString());
            if (url == null) {
                in.setError(getString(R.string.bad_address));
                return;
            }
            prefs.edit().putString(KEY_URL, url).apply();
            dialog.dismiss();
            errorShown = false;
            web.clearHistory();
            web.loadUrl(url);
        }));
        dialog.show();
    }

    private void showError(String what) {
        if (errorShown || isFinishing()) return;
        errorShown = true;
        if (dialog != null && dialog.isShowing()) dialog.dismiss();
        dialog = new AlertDialog.Builder(this)
                .setTitle(R.string.no_connection)
                .setMessage(getString(R.string.no_connection_msg, prefs.getString(KEY_URL, "?"), what))
                .setCancelable(false)
                .setPositiveButton(R.string.retry, (d, w) -> {
                    errorShown = false;
                    web.loadUrl(prefs.getString(KEY_URL, "about:blank"));
                })
                .setNeutralButton(R.string.change_address, (d, w) -> {
                    errorShown = false;
                    askAddress(true);
                })
                .setNegativeButton(R.string.close_app, (d, w) -> finish())
                .create();
        dialog.show();
    }

    private boolean sameServer(Uri u) {
        Uri base = Uri.parse(prefs.getString(KEY_URL, ""));
        return u.getHost() != null && u.getHost().equalsIgnoreCase(base.getHost()) && u.getPort() == base.getPort();
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest req) {
            Uri u = req.getUrl();
            if (("http".equals(u.getScheme()) || "https".equals(u.getScheme())) && sameServer(u)) return false;
            try {   // linki zewnetrzne (GitHub, mailto: itp.) w przegladarce / poczcie
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            } catch (ActivityNotFoundException ignored) {
            }
            return true;
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            view.setVisibility(View.VISIBLE);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) {
                view.loadUrl("about:blank");
                showError(String.valueOf(err.getDescription()));
            }
        }
    }

    /** Dostepne ze strony jako window.SigenApp (przycisk "Adres serwera" w stopce dashboardu). */
    private class Bridge {
        @JavascriptInterface
        public void changeAddress() {
            runOnUiThread(() -> askAddress(true));
        }

        @JavascriptInterface
        public String version() {
            return BuildConfigCompat.versionName(MainActivity.this);
        }
    }
}
