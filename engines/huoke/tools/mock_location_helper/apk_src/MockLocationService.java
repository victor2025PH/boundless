package com.openclaw.mocklocation;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.location.Location;
import android.location.LocationManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;

/**
 * MockLocationService — 后台服务，持续维持 Mock GPS 位置有效性。
 *
 * 某些 Android 系统在 BroadcastReceiver 处理完毕后会自动移除 TestProvider，
 * 导致位置设置只持续几秒。本服务通过每3秒重新设置一次位置来保持持续有效。
 *
 * 触发方式 (通常由 MockLocationReceiver 内部调用):
 *   adb shell am startservice -n com.openclaw.mocklocation/.MockLocationService \
 *       --ef latitude 40.7128 --ef longitude -74.0060
 *
 * 停止服务:
 *   adb shell am stopservice -n com.openclaw.mocklocation/.MockLocationService
 */
public class MockLocationService extends Service {

    private static final String TAG = "OpenClawMockSvc";
    private static final String PROVIDER = LocationManager.GPS_PROVIDER;
    private static final int CHANNEL_ID_INT = 9001;
    private static final String CHANNEL_ID = "mockloc_channel";
    private static final long UPDATE_INTERVAL_MS = 3000; // 每3秒更新一次

    private Handler mHandler;
    private Runnable mUpdateTask;
    private double mLatitude = 0;
    private double mLongitude = 0;
    private double mAltitude = 0;
    private float mAccuracy = 1.0f;
    private LocationManager mLocationManager;

    @Override
    public void onCreate() {
        super.onCreate();
        mLocationManager = (LocationManager) getSystemService(LOCATION_SERVICE);
        mHandler = new Handler(Looper.getMainLooper());
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null) {
            mLatitude = intent.getDoubleExtra("latitude",
                        intent.getFloatExtra("latitude", 0));
            mLongitude = intent.getDoubleExtra("longitude",
                         intent.getFloatExtra("longitude", 0));
            mAltitude = intent.getDoubleExtra("altitude",
                        intent.getFloatExtra("altitude", 0));
            mAccuracy = intent.getFloatExtra("accuracy", 1.0f);
        }

        // 启动前台服务（Android 8+ 要求）
        startForeground(CHANNEL_ID_INT, buildNotification());

        // 开始持续更新位置
        startLocationUpdates();

        Log.i(TAG, String.format("MockLocation服务已启动: (%.6f, %.6f)", mLatitude, mLongitude));
        return START_STICKY; // 被杀死后自动重启
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null; // 不支持绑定
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (mHandler != null && mUpdateTask != null) {
            mHandler.removeCallbacks(mUpdateTask);
        }
        // 清理 TestProvider
        try {
            mLocationManager.removeTestProvider(PROVIDER);
        } catch (Exception ignored) {}
        Log.i(TAG, "MockLocation服务已停止");
    }

    private void startLocationUpdates() {
        // 先注册 Provider
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                mLocationManager.addTestProvider(
                        PROVIDER, false, false, false, false, false,
                        true, true,
                        android.location.provider.ProviderProperties.POWER_USAGE_LOW,
                        android.location.provider.ProviderProperties.ACCURACY_FINE
                );
            } else {
                mLocationManager.addTestProvider(
                        PROVIDER, false, false, false, false, false,
                        true, true, 0, 0
                );
            }
            mLocationManager.setTestProviderEnabled(PROVIDER, true);
        } catch (Exception e) {
            Log.d(TAG, "Provider 初始化: " + e.getMessage());
        }

        // 定时更新任务
        mUpdateTask = new Runnable() {
            @Override
            public void run() {
                updateLocation();
                mHandler.postDelayed(this, UPDATE_INTERVAL_MS);
            }
        };
        mHandler.post(mUpdateTask);
    }

    private void updateLocation() {
        try {
            Location location = new Location(PROVIDER);
            location.setLatitude(mLatitude);
            location.setLongitude(mLongitude);
            location.setAltitude(mAltitude);
            location.setAccuracy(mAccuracy);
            location.setTime(System.currentTimeMillis());
            location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                location.setBearingAccuracyDegrees(0.1f);
                location.setSpeedAccuracyMetersPerSecond(0.01f);
                location.setVerticalAccuracyMeters(mAccuracy);
            }

            mLocationManager.setTestProviderLocation(PROVIDER, location);
        } catch (Exception e) {
            Log.w(TAG, "位置更新失败（可能Provider已被移除）: " + e.getMessage());
            // 重新尝试注册 Provider
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                    mLocationManager.addTestProvider(
                            PROVIDER, false, false, false, false, false,
                            true, true,
                            android.location.provider.ProviderProperties.POWER_USAGE_LOW,
                            android.location.provider.ProviderProperties.ACCURACY_FINE
                    );
                } else {
                    mLocationManager.addTestProvider(
                            PROVIDER, false, false, false, false, false,
                            true, true, 0, 0
                    );
                }
                mLocationManager.setTestProviderEnabled(PROVIDER, true);
            } catch (Exception ignored) {}
        }
    }

    private Notification buildNotification() {
        Notification.Builder builder;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            builder = new Notification.Builder(this, CHANNEL_ID);
        } else {
            builder = new Notification.Builder(this);
        }
        return builder
                .setContentTitle("OpenClaw MockLocation")
                .setContentText(String.format("模拟位置运行中: %.4f, %.4f", mLatitude, mLongitude))
                .setSmallIcon(android.R.drawable.ic_menu_mylocation)
                .build();
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "MockLocation Service",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("OpenClaw GPS 模拟位置后台服务");
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) nm.createNotificationChannel(channel);
        }
    }
}
