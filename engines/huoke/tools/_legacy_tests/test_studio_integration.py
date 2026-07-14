# -*- coding: utf-8 -*-
"""Content Studio 集成测试"""
import sys
import os

# 修复 Windows GBK 控制台编码
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def safe(s, max_len=60):
    """安全打印，截断并替换不可显示字符"""
    return str(s)[:max_len].encode('ascii', errors='replace').decode()

errors = []

# ── [1] 配置加载 ──────────────────────────────────────────
print("\n[1] 测试配置加载...")
try:
    import yaml
    with open("config/studio_config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    with open("config/personas.yaml", encoding="utf-8") as f:
        personas = yaml.safe_load(f)
    assert "studio" in cfg
    assert "personas" in personas
    assert "italy_lifestyle" in personas["personas"]
    print(f"  OK: studio_config loaded, mode={cfg['studio']['mode']}")
    print(f"  OK: personas loaded, count={len(personas['personas'])}")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[1] {e}")

# ── [2] 数据库初始化 ───────────────────────────────────────
print("\n[2] 测试数据库初始化...")
try:
    from src.studio.studio_db import init_studio_db, get_studio_stats
    init_studio_db()
    stats = get_studio_stats()
    print(f"  OK: DB initialized, stats={stats}")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[2] {e}")

# ── [3] 内容生成（模板模式）────────────────────────────────
print("\n[3] 测试内容生成（模板模式，无需API Key）...")
try:
    from src.studio.content_agent import generate_content_simple
    result = generate_content_simple('italy_lifestyle', 'tiktok', 'slideshow')
    assert result is not None
    assert 'hook' in result
    assert 'script' in result
    assert 'visual_prompts' in result
    print(f"  OK: hook={safe(result['hook'], 50)}")
    print(f"  OK: script_len={len(result.get('script',''))}")
    print(f"  OK: visual_prompts={len(result.get('visual_prompts', []))} shots")
    print(f"  OK: hashtags={result.get('hashtags', [])[:3]}")
except Exception as e:
    import traceback
    print(f"  FAIL: {e}")
    traceback.print_exc()
    errors.append(f"[3] {e}")

# ── [4] TTS 语音配置 ───────────────────────────────────────
print("\n[4] 测试TTS语音配置...")
try:
    from src.studio.tts_generator import get_voice_for_country
    voice_it = get_voice_for_country('italy', 'female')
    voice_br = get_voice_for_country('brazil', 'male')
    voice_global = get_voice_for_country('global', 'all')
    print(f"  OK: italy(female)={voice_it}")
    print(f"  OK: brazil(male)={voice_br}")
    print(f"  OK: global={voice_global}")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[4] {e}")

# ── [5] 国家配置 ───────────────────────────────────────────
print("\n[5] 测试国家配置...")
try:
    from src.studio.country_config import get_country_config, get_tts_voice
    cfg_it = get_country_config('italy')
    cfg_br = get_country_config('brazil')
    assert cfg_it is not None
    voice = get_tts_voice('italy', 'female')
    print(f"  OK: italy={cfg_it.get('language')}, tz={cfg_it.get('timezone')}")
    print(f"  OK: brazil={cfg_br.get('language') if cfg_br else 'N/A'}")
    print(f"  OK: italy_voice={voice}")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[5] {e}")

# ── [6] 发布者工厂 ─────────────────────────────────────────
print("\n[6] 测试发布者工厂...")
try:
    from src.studio.publishers import get_publisher
    pub_tiktok = get_publisher('tiktok')
    pub_instagram = get_publisher('instagram')
    pub_telegram = get_publisher('telegram')
    print(f"  OK: tiktok={type(pub_tiktok).__name__}")
    print(f"  OK: instagram={type(pub_instagram).__name__}")
    print(f"  OK: telegram={type(pub_telegram).__name__}")
    # 未实现平台应返回 None 或抛出异常
    try:
        pub_fb = get_publisher('facebook')
        print(f"  INFO: facebook={type(pub_fb).__name__ if pub_fb else 'None (Phase2)'}")
    except Exception:
        print(f"  INFO: facebook=NotImplemented (Phase2, expected)")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[6] {e}")

# ── [7] StudioManager 单例 ─────────────────────────────────
print("\n[7] 测试StudioManager单例...")
try:
    from src.studio.studio_manager import get_studio_manager
    mgr = get_studio_manager()
    assert mgr is not None
    # 测试 quick_generate_preview（只生成文案，不调用生成器）
    preview = mgr.quick_generate_preview('italy_lifestyle', 'tiktok')
    assert preview is not None
    print(f"  OK: StudioManager initialized")
    print(f"  OK: preview keys={list(preview.keys())[:5]}")
except Exception as e:
    import traceback
    print(f"  FAIL: {e}")
    traceback.print_exc()
    errors.append(f"[7] {e}")

# ── [8] API 路由注册检查 ────────────────────────────────────
print("\n[8] 测试Studio API路由...")
try:
    from src.host.routers.studio import router
    routes = [r.path for r in router.routes]
    print(f"  OK: routes={routes}")
    assert any('/studio' in r for r in routes), "No /studio routes found"
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[8] {e}")

# ── [9] 视频处理器导入 ─────────────────────────────────────
print("\n[9] 测试视频处理器导入...")
try:
    from src.studio.video_processor import create_slideshow, process_ai_video, get_platform_specs
    specs = get_platform_specs('tiktok')
    print(f"  OK: tiktok specs={specs}")
    specs_xhs = get_platform_specs('xiaohongshu')
    print(f"  OK: xiaohongshu specs={specs_xhs}")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[9] {e}")

# ── [10] 图片/视频生成器导入 ───────────────────────────────
print("\n[10] 测试图片/视频生成器导入...")
try:
    from src.studio.image_generator import generate_image, generate_image_batch, get_fal_key
    from src.studio.video_generator import generate_video, estimate_cost, get_aspect_ratio_for_platform
    cost_15s = estimate_cost(15, 'wan2.6')
    ar = get_aspect_ratio_for_platform('tiktok')
    print(f"  OK: image_generator imported")
    print(f"  OK: video cost 15s = ${cost_15s:.3f}")
    print(f"  OK: tiktok aspect_ratio={ar}")
    try:
        fal_key = get_fal_key()
        print(f"  INFO: FAL_KEY={'set' if fal_key else 'NOT SET (configure before real generation)'}")
    except Exception:
        print(f"  INFO: FAL_KEY=NOT SET (configure before real generation)")
except Exception as e:
    print(f"  FAIL: {e}")
    errors.append(f"[10] {e}")

# ── 总结 ───────────────────────────────────────────────────
print("\n" + "="*50)
if errors:
    print(f"FAILED: {len(errors)} error(s):")
    for err in errors:
        print(f"  - {err}")
    sys.exit(1)
else:
    print(f"ALL TESTS PASSED (10/10)")
    print("Content Studio 架构验证完成!")
    print("\n下一步:")
    print("  1. 配置 FAL_KEY 环境变量或 config/fal_key.txt")
    print("  2. 配置 OPENAI_API_KEY 启用CrewAI AI文案生成")
    print("  3. 连接Android设备测试ADB发布")
    print("  4. 访问 /studio/* API端点管理发布任务")
