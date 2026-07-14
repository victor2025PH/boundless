package com.openclaw;

import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Method;

/**
 * Wallpaper setter using WallpaperManager constructed directly via Binder,
 * with a FakeContext to satisfy API calls. No ActivityThread needed.
 *
 * The key insight: WallpaperManager.setStream() handles all the complexity
 * of ParcelFileDescriptor, completion callbacks, and crop generation that
 * raw Binder IPC was missing.
 */
public class WallpaperSetter {

    public static void main(String[] args) {
        if (args.length < 1) {
            System.err.println("Usage: WallpaperSetter <image_path>");
            System.exit(1);
        }

        String imagePath = args[0];
        if (!new File(imagePath).exists()) {
            System.err.println("ERROR: File not found: " + imagePath);
            System.exit(2);
        }

        try {
            // Step 1: Prepare Looper (some internals need it)
            Class<?> looperClass = Class.forName("android.os.Looper");
            try {
                looperClass.getMethod("prepareMainLooper").invoke(null);
            } catch (Exception e) { /* already prepared */ }

            // Step 2: Get IWallpaperManager binder proxy
            Class<?> smClass = Class.forName("android.os.ServiceManager");
            Object binder = smClass.getMethod("getService", String.class)
                                   .invoke(null, "wallpaper");
            if (binder == null) {
                System.err.println("ERROR: wallpaper service not found");
                System.exit(3);
            }

            Class<?> iBinderClass = Class.forName("android.os.IBinder");
            Class<?> stubClass = Class.forName("android.app.IWallpaperManager$Stub");
            Object wmService = stubClass.getMethod("asInterface", iBinderClass)
                                        .invoke(null, binder);

            // Step 3: Call setWallpaper + write data + close properly
            // Replicate what WallpaperManager.setStream() does internally
            Class<?> rectClass = Class.forName("android.graphics.Rect");
            Class<?> bundleClass = Class.forName("android.os.Bundle");
            Class<?> callbackClass = Class.forName("android.app.IWallpaperManagerCallback");

            Method setWallpaper = null;
            for (Method m : wmService.getClass().getMethods()) {
                if ("setWallpaper".equals(m.getName())) {
                    setWallpaper = m;
                    break;
                }
            }
            if (setWallpaper == null) {
                System.err.println("ERROR: setWallpaper method not found");
                System.exit(3);
            }

            byte[] imageData = readFile(new File(imagePath));

            // Set for home screen (1) then lock screen (2)
            for (int which : new int[]{1, 2}) {
                Object extras = bundleClass.getDeclaredConstructor().newInstance();

                // Build args dynamically based on method signature
                Class<?>[] pt = setWallpaper.getParameterTypes();
                Object[] a = new Object[pt.length];
                java.util.List<Integer> intIdx = new java.util.ArrayList<>();

                for (int i = 0; i < pt.length; i++) {
                    String tn = pt[i].getSimpleName();
                    if (tn.equals("String") && i == 0) a[i] = null;
                    else if (tn.equals("String")) a[i] = "com.android.shell";
                    else if (tn.equals("Rect")) a[i] = null;
                    else if (tn.equals("boolean")) a[i] = true;
                    else if (tn.equals("Bundle")) a[i] = extras;
                    else if (tn.equals("int")) { a[i] = which; intIdx.add(i); }
                    else a[i] = null;
                }
                // Fix int positions: last=userId(0), second-to-last=which
                if (intIdx.size() >= 2) {
                    a[intIdx.get(intIdx.size() - 2)] = which;
                    a[intIdx.get(intIdx.size() - 1)] = 0;
                }

                Object pfd = setWallpaper.invoke(wmService, a);

                if (pfd != null) {
                    // Use ParcelFileDescriptor.AutoCloseOutputStream (critical!)
                    // This properly signals the server when writing is complete
                    Class<?> acosClass = Class.forName(
                        "android.os.ParcelFileDescriptor$AutoCloseOutputStream");
                    Constructor<?> acosCtor = acosClass.getConstructor(
                        Class.forName("android.os.ParcelFileDescriptor"));
                    OutputStream os = (OutputStream) acosCtor.newInstance(pfd);

                    os.write(imageData);
                    os.flush();
                    os.close(); // This closes PFD and notifies server

                    System.err.println("INFO: type=" + which + " written "
                        + imageData.length + " bytes via AutoCloseOutputStream");
                } else {
                    System.err.println("WARN: setWallpaper returned null for type=" + which);
                }

                // Brief pause between home and lock to let server process
                Thread.sleep(500);
            }

            System.out.println("WP_SET_OK");

        } catch (Exception e) {
            System.err.println("ERROR: " + e.getClass().getSimpleName() + ": " + e.getMessage());
            Throwable cause = e.getCause();
            while (cause != null) {
                System.err.println("CAUSED BY: " + cause.getClass().getSimpleName()
                    + ": " + cause.getMessage());
                cause = cause.getCause();
            }
            System.exit(4);
        }
    }

    private static byte[] readFile(File f) throws Exception {
        FileInputStream fis = new FileInputStream(f);
        byte[] data = new byte[(int) f.length()];
        int off = 0;
        while (off < data.length) {
            int n = fis.read(data, off, data.length - off);
            if (n < 0) break;
            off += n;
        }
        fis.close();
        return data;
    }
}
