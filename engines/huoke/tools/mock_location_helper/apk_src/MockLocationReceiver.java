package com.openclaw.mocklocation;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.location.Location;
import android.location.LocationManager;
import android.os.Build;
import android.os.SystemClock;
import android.util.Log;

/**
 * BroadcastReceiver — 接收 ADB 广播，设置 Mock GPS 位置。
 *
 * 支持两种触发方式:
 *
 * 方式1: 精确参数（推荐）
 *   adb shell am broadcast -a com.openclaw.SET_MOCK_LOCATION \
 *       --ef latitude 40.7128 --ef longitude -74.0060 --ef altitude 0.0 \
 *       -n com.openclaw.mocklocation/.MockLocationReceiver
 *
 * 方式2: 字符串参数（兼容某些系统版本）
 *   adb shell am broadcast -a com.openclaw.SET_MOCK_LOCATION \
 *       --es lat "40.7128" --es lng "-74.0060" \
 *       -n com.openclaw.mocklocation/.MockLocationReceiver
 *
 * 前提: 设备开发者选项 > 选择模拟位置应用 = 本应用
 */
public class MockLocationReceiver extends BroadcastReceiver {

    private static final String TAG = "OpenClawMockLoc";
    private static final String PROVIDER = LocationManager.GPS_PROVIDER;

    // 广播结果码
    private static final int RESULT_OK = 0;
    private static final int RESULT_ERR_PERMISSION = 10;
    private static final int RESULT_ERR_PROVIDER = 11;
    private static final int RESULT_ERR_SET_LOCATION = 12;
    private static final int RESULT_ERR_NO_PARAMS = 13;

    @Override
    public void onReceive(Context context, Intent intent) {
        Log.d(TAG, "收到广播: " + intent.getAction());

        // 解析经纬度（支持 float/double/String 三种参数类型）
        double latitude = getDoubleParam(intent, "latitude", "lat");
        double longitude = getDoubleParam(intent, "longitude", "lng");
        double altitude = getDoubleParam(intent, "altitude", "alt");
        float accuracy = (float) getDoubleParam(intent, "accuracy", "acc");
        if (accuracy <= 0) accuracy = 1.0f;

        // 参数验证
        if (latitude == Double.MIN_VALUE || longitude == Double.MIN_VALUE) {
            Log.e(TAG, "缺少必要参数: latitude 和 longitude");
            setResultCode(RESULT_ERR_NO_PARAMS);
            setResultData("FAIL: 缺少 latitude/longitude 参数");
            return;
        }

        if (latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) {
            Log.e(TAG, "坐标超出范围: " + latitude + ", " + longitude);
            setResultCode(RESULT_ERR_NO_PARAMS);
            setResultData("FAIL: 坐标超出有效范围");
            return;
        }

        // 获取 LocationManager
        LocationManager lm = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (lm == null) {
            Log.e(TAG, "无法获取 LocationManager");
            setResultCode(RESULT_ERR_PROVIDER);
            setResultData("FAIL: LocationManager 不可用");
            return;
        }

        try {
            // 注册 MockProvider（必须先添加才能设置位置）
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                // Android 12+ 使用新 API
                lm.addTestProvider(
                        PROVIDER,
                        false, false, false, false, false,
                        true, true,
                        android.location.provider.ProviderProperties.POWER_USAGE_LOW,
                        android.location.provider.ProviderProperties.ACCURACY_FINE
                );
            } else {
                // Android 11 及以下
                lm.addTestProvider(
                        PROVIDER,
                        false, false, false, false, false,
                        true, true, 0, 0
                );
            }
            lm.setTestProviderEnabled(PROVIDER, true);

        } catch (SecurityException e) {
            Log.e(TAG, "没有 MockLocation 权限，请在开发者选项中授权本应用: " + e.getMessage());
            setResultCode(RESULT_ERR_PERMISSION);
            setResultData("FAIL: 权限不足 - 请在「开发者选项 > 选择模拟位置应用」中选择 OpenClaw MockLoc");
            return;
        } catch (IllegalArgumentException e) {
            // Provider 已存在，继续（正常情况）
            Log.d(TAG, "Provider 已存在，继续设置位置");
        }

        // 构造 Location 对象
        Location location = new Location(PROVIDER);
        location.setLatitude(latitude);
        location.setLongitude(longitude);
        location.setAltitude(altitude);
        location.setAccuracy(accuracy);
        location.setTime(System.currentTimeMillis());
        location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());

        // Android 8+ 需要设置 bearing accuracy 和 speed accuracy
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            location.setBearingAccuracyDegrees(0.1f);
            location.setSpeedAccuracyMetersPerSecond(0.01f);
            location.setVerticalAccuracyMeters(accuracy);
        }

        try {
            lm.setTestProviderLocation(PROVIDER, location);
            Log.i(TAG, String.format("Mock位置已设置: (%.6f, %.6f) alt=%.1f acc=%.1f",
                    latitude, longitude, altitude, accuracy));
            setResultCode(RESULT_OK);
            setResultData(String.format("OK: (%.6f, %.6f)", latitude, longitude));
        } catch (Exception e) {
            Log.e(TAG, "设置位置失败: " + e.getMessage());
            setResultCode(RESULT_ERR_SET_LOCATION);
            setResultData("FAIL: " + e.getMessage());
        }

        // 启动 MockLocationService 保持位置持续有效（可选）
        // 如果 broadcast 设置的位置在短时内被系统覆盖，取消注释以下代码
        /*
        Intent serviceIntent = new Intent(context, MockLocationService.class);
        serviceIntent.putExtra("latitude", latitude);
        serviceIntent.putExtra("longitude", longitude);
        serviceIntent.putExtra("altitude", altitude);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.startForegroundService(serviceIntent);
        } else {
            context.startService(serviceIntent);
        }
        */
    }

    /**
     * 从 Intent 中读取 double 参数，支持 float (ef)、double (ed)、String (es) 三种类型。
     */
    private double getDoubleParam(Intent intent, String key1, String key2) {
        // 尝试 float 参数 (--ef)
        if (intent.hasExtra(key1)) {
            try {
                return intent.getFloatExtra(key1, Float.MIN_VALUE);
            } catch (Exception ignored) {}
        }
        if (intent.hasExtra(key2)) {
            try {
                return intent.getFloatExtra(key2, Float.MIN_VALUE);
            } catch (Exception ignored) {}
        }

        // 尝试 double 参数 (--ed)
        try {
            Bundle extras = intent.getExtras();
            if (extras != null) {
                if (extras.containsKey(key1)) return extras.getDouble(key1, Double.MIN_VALUE);
                if (extras.containsKey(key2)) return extras.getDouble(key2, Double.MIN_VALUE);
            }
        } catch (Exception ignored) {}

        // 尝试 String 参数 (--es)
        String str = intent.getStringExtra(key1);
        if (str == null) str = intent.getStringExtra(key2);
        if (str != null) {
            try {
                return Double.parseDouble(str.trim());
            } catch (NumberFormatException ignored) {}
        }

        return Double.MIN_VALUE;
    }
}
