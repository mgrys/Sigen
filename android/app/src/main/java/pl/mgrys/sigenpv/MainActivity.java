package pl.mgrys.sigenpv;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.Html;
import android.text.InputType;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
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
 * "Adres serwera" w stopce dashboardu albo na stronie bledu polaczenia.
 *
 * Brak polaczenia (np. restart aplikacji na komputerze) -> strona bledu z przyciskami i ponawianiem co 10 s,
 * nigdy pusty (czarny) ekran. Po powrocie do aplikacji po dluzszej przerwie dashboard jest odswiezany.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "sigen", KEY_URL = "url";
    private static final long RETRY_MS = 10_000, STALE_MS = 10 * 60_000;
    private FrameLayout root;
    private WebView web;
    private SharedPreferences prefs;
    private AlertDialog dialog;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean errorPage;          // wyswietlana strona bledu (ponawianie)
    private boolean loadedOk;           // dashboard zaladowany
    private long pausedAt;
    private final Runnable retry = () -> {
        if (errorPage && !isFinishing()) loadDashboard();
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(31, 30, 29));
        setContentView(root);
        CookieManager.getInstance().setAcceptCookie(true);   // sesja po wpisaniu PIN-u
        createWebView();
        if (prefs.getString(KEY_URL, null) == null) {
            askAddress(false);
        } else {
            loadDashboard();
        }
    }

    private void createWebView() {
        if (web != null) {
            root.removeView(web);
            web.destroy();
        }
        web = new WebView(this);
        web.setLayoutParams(new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        web.setBackgroundColor(Color.rgb(31, 30, 29));
        root.addView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // dashboard pamieta ukryte serie wykresow itp. (localStorage)
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);   // zawsze aktualna strona po aktualizacji aplikacji na komputerze
        s.setUserAgentString(s.getUserAgentString() + " SigenApp/" + BuildConfigCompat.versionName(this));
        web.addJavascriptInterface(new Bridge(), "SigenApp");
        web.setWebChromeClient(new WebChromeClient());       // okna potwierdzen (confirm/alert) ze strony
        web.setWebViewClient(new Client());
    }

    private void loadDashboard() {
        ui.removeCallbacks(retry);
        String url = prefs.getString(KEY_URL, null);
        if (url == null) {
            askAddress(false);
            return;
        }
        errorPage = false;
        loadedOk = false;
        web.loadUrl(url);
    }

    @Override
    protected void onPause() {
        super.onPause();
        pausedAt = SystemClock.elapsedRealtime();
        CookieManager.getInstance().flush();
        web.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        boolean stale = pausedAt > 0 && SystemClock.elapsedRealtime() - pausedAt > STALE_MS;
        String cur = web.getUrl();
        boolean blank = cur == null || cur.startsWith("about:") || cur.startsWith("data:");
        if (prefs.getString(KEY_URL, null) != null && (errorPage || stale || (blank && !loadedOk))
                && (dialog == null || !dialog.isShowing())) {
            loadDashboard();
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(retry);
        if (web != null) web.destroy();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (!errorPage && web.canGoBack()) {
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
            web.clearHistory();
            loadDashboard();
        }));
        dialog.show();
    }

    /** Strona bledu w oknie (zamiast pustego ekranu) + ponawianie co RETRY_MS. */
    private void showError(String what) {
        if (isFinishing()) return;
        errorPage = true;
        loadedOk = false;
        String url = Html.escapeHtml(prefs.getString(KEY_URL, "?"));
        String html = "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;padding:28px 22px;font:16px/1.45 system-ui,sans-serif;background:#1f1e1d;color:#f0efec}"
                + "h1{font-size:20px;margin:0 0 12px}p{color:#c3c2b7}.u{color:#f0efec;word-break:break-all}"
                + "button{font:inherit;padding:11px 16px;margin:14px 10px 0 0;border-radius:10px;border:1px solid #555;"
                + "background:#2a2927;color:#f0efec}.p{background:#2a78d6;border-color:#2a78d6}"
                + "small{display:block;margin-top:22px;color:#8d8c86}</style></head><body>"
                + "<h1>" + Html.escapeHtml(getString(R.string.no_connection)) + "</h1>"
                + "<p>Adres: <span class='u'>" + url + "</span><br>Błąd: " + Html.escapeHtml(what) + "</p>"
                + "<p>Sprawdź, czy komputer z aplikacją jest włączony i czy telefon jest w tej samej sieci Wi-Fi. "
                + "Po restarcie aplikacji na komputerze połączenie wróci samo.</p>"
                + "<button class='p' onclick='SigenApp.retry()'>" + Html.escapeHtml(getString(R.string.retry)) + "</button>"
                + "<button onclick='SigenApp.changeAddress()'>" + Html.escapeHtml(getString(R.string.change_address)) + "</button>"
                + "<small>Ponawiam automatycznie co 10 s…</small></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        ui.removeCallbacks(retry);
        ui.postDelayed(retry, RETRY_MS);
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
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (!errorPage && url != null && url.startsWith("http")) loadedOk = true;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
            if (req.isForMainFrame()) showError(String.valueOf(err.getDescription()));
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest req, WebResourceResponse resp) {
            if (req.isForMainFrame() && resp.getStatusCode() >= 500) showError("HTTP " + resp.getStatusCode());
        }

        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            // proces strony padl (np. brak pamieci) - nowe okno zamiast zamkniecia aplikacji / czarnego ekranu
            ui.post(() -> {
                createWebView();
                loadDashboard();
            });
            return true;
        }
    }

    /** Dostepne ze strony jako window.SigenApp (przycisk "Adres serwera" w stopce dashboardu). */
    private class Bridge {
        @JavascriptInterface
        public void changeAddress() {
            runOnUiThread(() -> askAddress(true));
        }

        @JavascriptInterface
        public void retry() {
            runOnUiThread(MainActivity.this::loadDashboard);
        }

        @JavascriptInterface
        public String version() {
            return BuildConfigCompat.versionName(MainActivity.this);
        }
    }
}
