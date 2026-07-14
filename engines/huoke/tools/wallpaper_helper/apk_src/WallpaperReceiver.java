package com.openclaw.wallpaperhelper;

import android.app.WallpaperManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import java.io.File;
import java.io.IOException;

/**
 * BroadcastReceiver that sets wallpaper when triggered via ADB:
 *
 *   adb shell am broadcast -a com.openclaw.SET_WALLPAPER \
 *       --es path "/sdcard/Download/openclaw_wallpaper.png" \
 *       -n com.openclaw.wallpaperhelper/.WallpaperReceiver
 *
 * Sets both home screen and lock screen wallpaper.
 */
public class WallpaperReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        String path = intent.getStringExtra("path");
        if (path == null || path.isEmpty()) {
            path = "/sdcard/Download/openclaw_wallpaper.png";
        }

        File file = new File(path);
        if (!file.exists()) {
            setResultCode(2);
            setResultData("File not found: " + path);
            return;
        }

        try {
            Bitmap bitmap = BitmapFactory.decodeFile(path);
            if (bitmap == null) {
                setResultCode(3);
                setResultData("Failed to decode: " + path);
                return;
            }

            WallpaperManager wm = WallpaperManager.getInstance(context);
            // FLAG_SYSTEM (1) = home screen, FLAG_LOCK (2) = lock screen
            wm.setBitmap(bitmap, null, true, WallpaperManager.FLAG_SYSTEM | WallpaperManager.FLAG_LOCK);
            bitmap.recycle();

            setResultCode(0);
            setResultData("OK");
        } catch (IOException e) {
            setResultCode(4);
            setResultData("Error: " + e.getMessage());
        }
    }
}
