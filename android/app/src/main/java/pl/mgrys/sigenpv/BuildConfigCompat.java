package pl.mgrys.sigenpv;

import android.content.Context;
import android.content.pm.PackageManager;

/** Wersja aplikacji z manifestu (bez generowania klasy BuildConfig). */
final class BuildConfigCompat {
    private BuildConfigCompat() {
    }

    static String versionName(Context c) {
        try {
            return c.getPackageManager().getPackageInfo(c.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }
}
