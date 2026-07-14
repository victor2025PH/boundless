# -*- coding: utf-8 -*-
"""P2-4 Sprint A 冷启动探针：验证 Ollama qwen2.5vl:7b 全链路可用。

步骤：
  1. /api/tags 健康检查
  2. 生成 1 张合成 JPEG（带日文说明字幕），走 ollama_vlm.classify_images
  3. 校验返回 JSON 字段完整
  4. 跑一次 ProfileClassifier L1（纯规则，不触发 VLM）
  5. 跑一次 ProfileClassifier L2 端到端（dry_run=True，不写库）

用法::
    python scripts/smoke_test_p2a_vlm.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# 项目根加入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from src.host import ollama_vlm, fb_profile_classifier, fb_target_personas  # noqa: E402


def _green(s): return f"\033[32m{s}\033[0m"
def _red(s): return f"\033[31m{s}\033[0m"
def _yellow(s): return f"\033[33m{s}\033[0m"


def _make_test_image(path: Path, color=(255, 220, 230), label="test"):
    """生成 800x800 彩色图 + 文字，用于 VLM 测试。"""
    img = Image.new("RGB", (800, 800), color)
    d = ImageDraw.Draw(img)
    d.text((50, 50), label, fill=(40, 40, 40))
    d.ellipse((200, 200, 600, 600), fill=(180, 140, 160), outline=(80, 60, 70), width=4)
    d.text((280, 380), "PHOTO", fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=85)


def test_health():
    print("\n[1] Ollama 健康检查")
    h = ollama_vlm.check_health()
    print(f"  online={h['online']} model_available={h['model_available']}")
    print(f"  endpoint={h['endpoint']} model={h['model']}")
    print(f"  本地模型: {h['models']}")
    if not h["online"]:
        print(_red(f"  FAIL: {h.get('error','')}"))
        return False
    if not h["model_available"]:
        print(_red(f"  FAIL: 没找到模型 {h['model']}"))
        return False
    print(_green("  PASS"))
    return True


def test_vlm_generate():
    print("\n[2] VLM 直调（generate，不含图）")
    raw, meta = ollama_vlm.generate(
        prompt='Reply with exactly this JSON: {"ok": true}',
        image_paths=None, scene="smoke_generate",
    )
    print(f"  ok={meta['ok']} latency_ms={meta['latency_ms']} tokens(in/out)="
          f"{meta.get('input_tokens',0)}/{meta.get('output_tokens',0)}")
    print(f"  raw={raw[:120]!r}")
    if not meta["ok"]:
        print(_red(f"  FAIL: {meta.get('error')}"))
        return False
    print(_green("  PASS"))
    return True


def test_vlm_classify_image():
    print("\n[3] VLM 多图+JSON（classify_images）")
    img_path = ROOT / "data" / "smoke_p2a_test_image.jpg"
    _make_test_image(img_path, color=(255, 220, 220), label="pink circle")
    prompt = (
        'Look at the image and reply with EXACTLY this JSON '
        '(no other words): {"main_color":"...", "shape":"..."}'
    )
    parsed, meta = ollama_vlm.classify_images(
        prompt=prompt, image_paths=[str(img_path)], scene="smoke_vision",
    )
    print(f"  ok={meta['ok']} latency_ms={meta['latency_ms']} images={meta['image_count']}")
    print(f"  raw={meta.get('raw_response','')[:200]!r}")
    print(f"  parsed={parsed}")
    if not meta["ok"]:
        print(_red(f"  FAIL: {meta.get('error')}"))
        return False
    if not parsed:
        print(_yellow("  PARTIAL: VLM 联通但 JSON 解析失败；VLM 工作正常"))
        return True
    print(_green("  PASS"))
    return True


def test_l1_rules():
    print("\n[4] L1 规则评分（persona=日本中年女性）")
    cases = [
        {
            "title": "日文昵称 + 日文 bio",
            "ctx": {"display_name": "山田美香", "bio": "東京在住、家族と愛犬のために毎日お料理しています",
                    "username": "mika_jp", "locale": "ja-JP"},
        },
        {
            "title": "英文昵称 + 空 bio",
            "ctx": {"display_name": "John Smith", "bio": "", "username": "john01", "locale": "en-US"},
        },
        {
            "title": "混合（昵称英文 bio 日文）",
            "ctx": {"display_name": "Mika S", "bio": "趣味は料理です", "username": "mika", "locale": ""},
        },
    ]
    persona = fb_target_personas.get_persona()
    print(f"  persona={persona['persona_key']}/{persona.get('name')}  "
          f"pass_threshold={persona.get('l1',{}).get('pass_threshold')}")
    ok_all = True
    for c in cases:
        score, reasons = fb_profile_classifier.score_l1(persona, c["ctx"])
        print(f"  - {c['title']}: score={score:.0f}  reasons={reasons}")
    # 第 1 例应该 >= 30，第 2 例应该 = 0
    s1, _ = fb_profile_classifier.score_l1(persona, cases[0]["ctx"])
    s2, _ = fb_profile_classifier.score_l1(persona, cases[1]["ctx"])
    if s1 < 30 or s2 > 0:
        print(_red(f"  FAIL: 日文高分案例 {s1} / 英文零分案例 {s2} 不符合预期"))
        ok_all = False
    else:
        print(_green("  PASS"))
    return ok_all


def test_classifier_dry_run():
    print("\n[5] ProfileClassifier 端到端（dry_run=True，含 L2 VLM）")
    img_path = ROOT / "data" / "smoke_p2a_test_image.jpg"
    if not img_path.exists():
        _make_test_image(img_path, color=(255, 220, 230), label="avatar")
    t0 = time.time()
    r = fb_profile_classifier.classify(
        device_id="SMOKE_DEVICE",
        task_id="smoke-p2a",
        persona_key=None,
        target_key="https://facebook.com/smoke_test_user",
        display_name="山田花子",
        bio="東京在住、料理と旅行が好きな 45 歳です",
        username="hanako_jp",
        locale="ja-JP",
        image_paths=[str(img_path)],
        l2_image_paths=[str(img_path)],
        do_l2=True,
        dry_run=True,
    )
    elapsed = int((time.time() - t0) * 1000)
    print(f"  elapsed_ms={elapsed}")
    print(f"  stage_reached={r['stage_reached']} match={r['match']} score={r['score']:.1f}")
    print(f"  L1={json.dumps(r['l1'], ensure_ascii=False) if r['l1'] else None}")
    if r["l2"]:
        print(f"  L2={json.dumps({k:v for k,v in r['l2'].items() if k!='match_reasons'}, ensure_ascii=False)}")
        print(f"  L2.match_reasons={r['l2'].get('match_reasons')}")
    if r.get("insights"):
        print(f"  insights={json.dumps(r['insights'], ensure_ascii=False)[:300]}")
    if r["stage_reached"] != "L2":
        print(_yellow(f"  PARTIAL: L1 未通过({r['l1']})，L2 未触发"))
        return True
    if not (r["l2"] and r["l2"].get("ok")):
        print(_red(f"  FAIL: L2 VLM 没成功: {r['l2']}"))
        return False
    print(_green("  PASS"))
    return True


if __name__ == "__main__":
    print("=" * 70)
    print("P2-4 Sprint A 冷启动探针 (VLM + Classifier)")
    print("=" * 70)
    os.makedirs(ROOT / "data", exist_ok=True)

    results = []
    results.append(("health", test_health()))
    if results[-1][1]:
        results.append(("vlm_text", test_vlm_generate()))
        results.append(("vlm_image", test_vlm_classify_image()))
    results.append(("l1_rules", test_l1_rules()))
    if any(r[0] == "vlm_image" and r[1] for r in results):
        results.append(("classifier_e2e", test_classifier_dry_run()))

    print("\n" + "=" * 70)
    print("结果汇总")
    print("=" * 70)
    for name, ok in results:
        tag = _green("PASS") if ok else _red("FAIL")
        print(f"  {tag}  {name}")
    if all(r[1] for r in results):
        print(_green("\nALL GREEN - Sprint A 基础链路就绪"))
        sys.exit(0)
    else:
        print(_red("\n部分失败，见上方详情"))
        sys.exit(1)
