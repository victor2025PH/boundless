# -*- coding: utf-8 -*-
"""Look Pack 定妆包（C-5 妆容定妆 + 直播妆容层 + 试衣管线）离线门禁。
静态检查：不起服务、不吃 GPU；跑法: python test_look_pack.py"""
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
FAIL = []


def ok(msg):
    print(f"  [OK] {msg}")


def ng(msg):
    print(f"  [NG] {msg}")
    FAIL.append(msg)


def _has(src: str, needle: str, label: str):
    if needle in src:
        ok(label)
    else:
        ng(f"缺少 {label} ({needle})")


def test_makeup_api():
    p = ROOT / "makeup_api.py"
    if not p.exists():
        ng("makeup_api.py 不存在")
        return
    src = p.read_text(encoding="utf-8")
    _has(src, '@app.post("/makeup_transfer")', "makeup_api /makeup_transfer")
    _has(src, '@app.get("/makeup_styles")', "makeup_api /makeup_styles")
    _has(src, '@app.post("/makeup_extract")', "makeup_api /makeup_extract 参考色提取")
    _has(src, "service_auth.secure", "makeup_api 服务面鉴权接入")
    _has(src, "model_asset_buffer", "makeup_api 中文路径 buffer 加载(GBK 坑防御)")
    _has(src, "_redness_weight", "makeup_api 唇彩红度加权(防染牙)")
    _has(src, "port=8004", "makeup_api 监听 8004(8003 已被 faceswap2 预留)")


def test_app_config_registered():
    cfg = (ROOT / "app_config.py").read_text(encoding="utf-8")
    _has(cfg, '"makeup"', "app_config 登记 makeup 服务")
    _has(cfg, "makeup_api.py", "app_config makeup 脚本路径")


def test_hub_look_pack():
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "/api/profiles/{name}/makeup_preset", "hub 妆容定妆端点")
    _has(hub, "/api/profiles/{name}/look_pack", "hub 一键定妆包端点")
    _has(hub, "/api/profiles/{name}/tryon_preset", "hub 试衣定妆端点")
    _has(hub, "/api/makeup/styles", "hub 妆容样式代理")
    _has(hub, '"makeup":      _svc_url("makeup"', "hub SERVICES 注册 makeup(8004)")
    _has(hub, "face_hair_b64", "hub 链式基底 face_hair_b64(妆容重跑不丢发型)")
    _has(hub, 'profile.get("live_makeup")', "hub /faceswap 注入直播妆容层")
    _has(hub, '"live_makeup" in body', "hub PATCH live_makeup 配置(merge)")
    _has(hub, "imageio_ffmpeg", "hub 试衣静帧→循环视频(ffmpeg)")


def test_faceswap_live_makeup():
    fs = (ROOT / "faceswap_api.py").read_text(encoding="utf-8")
    _has(fs, "def _apply_live_makeup", "faceswap 直播妆容层函数")
    _has(fs, "makeup: dict | None = None", "faceswap SwapRequest.makeup 字段")
    _has(fs, "makeup_ms", "faceswap 响应 makeup_ms 观测")
    _has(fs, "_MAKEUP_WORK_SIDE", "faceswap 妆容降采样工作区(实测 61ms→10ms)")
    _has(fs, "req.makeup and faces_used > 0", "faceswap 默认关=零回归门条件")


def test_tryon_backend_hook():
    t = (ROOT / "tryon_api.py").read_text(encoding="utf-8")
    _has(t, "def load_fitdit_model", "tryon FitDiT 升级钩子")
    _has(t, "TRYON_BACKEND", "tryon 后端选择环境变量")
    _has(t, '"backend": _backend', "tryon /health 汇报真实后端")
    _has(t, 'mode == "fitdit"', "tryon 路由 FitDiT 分支")
    _has(t, "FitDiTWrapper", "tryon 引用 fitdit_pipeline 包装")


def test_stage3_fitdit_pipeline():
    """阶段3：FitDiT 真后端落地（权重 8.1G + 独立 env + onnx 预处理）。"""
    p = ROOT / "fitdit_pipeline.py"
    if not p.exists():
        ng("fitdit_pipeline.py 缺失")
        return
    src = p.read_text(encoding="utf-8")
    _has(src, "class FitDiTWrapper", "fitdit_pipeline 包装类")
    _has(src, "enable_model_cpu_offload", "fitdit_pipeline offload 显存策略")
    _has(src, "StableDiffusion3TryOnPipeline", "fitdit_pipeline SD3 试衣管线")
    _has(src, "_make_mask", "fitdit_pipeline mask 两步合一")
    bat = ROOT / "start_tryon_api.bat"
    if bat.exists() and "fitdit" in bat.read_text(encoding="utf-8", errors="replace"):
        ok("start_tryon_api.bat 独立启动器(fitdit env)")
    else:
        ng("start_tryon_api.bat 缺失或未指向 fitdit env")
    cfg = (ROOT / "app_config.py").read_text(encoding="utf-8")
    _has(cfg, '"env": "fitdit"', "app_config tryon 宿主 env=fitdit")


def test_stage3_idle_noise():
    """阶段3：Ditto 静默驱动底噪修正（纯零→-80dB 噪声，嘴部虚假动作减半）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "standard_normal", "hub _silence_wav 掺底噪(防 HuBERT 分布外幻觉嘴型)")
    _has(hub, "default_rng(7)", "hub _silence_wav 固定种子(可复现)")


def test_stage4_fitting_room():
    """阶段4：开播页试衣间面板 + 同源代理端点。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '@app.get("/api/tryon/clothes")', "hub 服装库代理")
    _has(hub, '@app.get("/api/tryon/cloth_thumb")', "hub 服装缩略图代理")
    _has(hub, '@app.post("/api/tryon/upload_cloth")', "hub 服装上传代理")
    _has(hub, '@app.post("/api/tryon/preview")', "hub 试穿预览(只出图不落库)")
    t = (ROOT / "tryon_api.py").read_text(encoding="utf-8")
    _has(t, "resolution:   str", "tryon 请求带清晰度档位")
    _has(t, "_infer_lock", "tryon 推理串行锁(防并发显存叠峰)")
    _has(t, "def _vram_gate", "tryon 显存准入闸(防 sysmem 回落假忙碌)")
    _has(t, "_RES_FREE_GB", "tryon 档位阶梯显存阈值")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("loadFittingClothes", "服装库装载"),
                          ("runFittingPreview", "试穿预览"),
                          ("applyFittingToProfile", "写入角色底片"),
                          ("uploadFittingCloth", "上传服装")]:
        _has(js, needle, f"hub.js {label}")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "试衣间", "ui.html 试衣间面板")
    _has(html, "fitting.resolution", "ui.html 清晰度档位选择")


def test_stage4_idle_motion():
    """阶段4：待机微动独立入口（任意角色照→Ditto 微动 idle_video）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '@app.post("/api/profiles/{name}/idle_motion")', "hub idle_motion 端点")
    _has(hub, '"tryon", "styled", "face"', "hub idle_motion 照片来源回退链")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "runIdleMotion", "hub.js 待机微动动作")


def test_stage5_cloth_extract():
    """阶段5：截图抠衣（人体解析/背景差分双路径 + part 限定 + 入库）。"""
    ta = (ROOT / "tryon_api.py").read_text(encoding="utf-8")
    _has(ta, '@app.post("/clothes/extract")', "tryon 抠衣端点")
    _has(ta, "_extract_by_parsing", "tryon 人体解析路径")
    _has(ta, "_extract_by_bgdiff", "tryon 背景差分兜底")
    _has(ta, "_PART_CLASSES", "tryon part 部位限定")
    _has(ta, "_compose_white", "tryon 白底合成(羽化)")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '@app.post("/api/tryon/extract_cloth")', "hub 抠衣代理")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "extractFittingCloth", "hub.js 截图抠衣动作")
    # 阶段7 起抠衣部位跟随「部位」选择器（默认 upper），不再写死
    _has(js, "part: this.fitting.clothType || 'upper'", "hub.js 抠衣部位跟随选择器")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "截图抠衣", "ui.html 抠衣按钮")


def test_stage6_asset_pipeline():
    """阶段6：素材种子包（导入工具+预设扩容+库管理）。"""
    imp = (ROOT / "tools" / "asset_import.py").read_text(encoding="utf-8")
    _has(imp, "TryOnVirtual/VITON-HD-TEST", "导入工具数据源")
    _has(imp, "_face_scores", "发型参考 MediaPipe 质检")
    _has(imp, "演示上衣", "服装演示前缀（与自家商品区分）")
    _has(imp, "演示发型", "发型演示前缀")
    mk = (ROOT / "makeup_api.py").read_text(encoding="utf-8")
    for s in ("斩男红梨", "女团紫", "欧美深邃", "蜜桃奶油", "雾面脏橘"):
        _has(mk, f'"{s}"', f"妆容预设 {s}")
    ta = (ROOT / "tryon_api.py").read_text(encoding="utf-8")
    _has(ta, '@app.post("/clothes/delete")', "tryon 删服装端点")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '@app.post("/api/tryon/delete_cloth")', "hub 删服装代理")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "fittingFiltered", "hub.js 服装搜索过滤")
    _has(js, "fittingPageItems", "hub.js 服装分页")
    _has(js, "deleteFittingCloth", "hub.js 右键删服装")
    _has(js, "fittingExtractProg", "hub.js 批量抠衣进度")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "搜服装名", "ui.html 搜索框")
    _has(html, 'multiple class="hidden" @change="extractFittingCloth', "ui.html 抠衣多选")


def test_stage7_lower_dress():
    """阶段7：下装/连衣裙试穿（DressCode 素材 + 部位选择 + 全链路透传）。"""
    imp = (ROOT / "tools" / "asset_import.py").read_text(encoding="utf-8")
    _has(imp, "def import_dresscode", "导入工具 dresscode 模式")
    _has(imp, "演示裤装", "下装演示前缀")
    _has(imp, "演示连衣裙", "连衣裙演示前缀")
    _has(imp, "test_pairs_unpaired.txt", "类别真值来自 pairs 文件")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    assert hub.count('req_body["cloth_type"]') >= 2, "hub 预览+定妆双端点 cloth_type 透传"
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "clothType:'upper'", "hub.js 部位状态默认上装")
    _has(js, "cloth_type: this.fitting.clothType", "hub.js 部位透传")
    _has(js, "part: this.fitting.clothType", "hub.js 抠衣部位跟随")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, '<option value="dress">连衣裙</option>', "ui.html 部位选择器")


def test_stage7_hair_ready():
    """阶段7：发型链就绪（懒加载 + 免编译补丁 + 演示发型库）。"""
    hair = (ROOT / "hair_api.py").read_text(encoding="utf-8")
    _has(hair, "_load_tried", "hair_api 懒加载一次性标记")
    _has(hair, 'load_hair_model()          # 懒加载', "hair_transfer 内懒加载")
    _has(hair, '"4" if hair_fast is not None else "10"', "冷载/驻留双阈值闸")
    op = (ROOT / "HairFastGAN" / "models" / "stylegan2" / "op" / "fused_act.py"
          ).read_text(encoding="utf-8")
    _has(op, "_HAS_COMPILED", "stylegan2 op 免编译兜底")
    for sub in ("encoder4editing/models/stylegan2/op",
                "FeatureStyleEncoder/pixel2style2pixel/models/stylegan2/op"):
        init = (ROOT / "HairFastGAN" / "models" / sub / "__init__.py").read_text(encoding="utf-8")
        _has(init, "from models.stylegan2.op", f"{sub.split('/')[0]} op 重定向主副本")
    n_hair = len(list((ROOT / "hair_styles").glob("演示发型*.jpg")))
    assert n_hair >= 30, f"演示发型库应≥30 张（现 {n_hair}）"


def test_stage8_hair_vram_autoreturn():
    """阶段8：发型显存自动归还（空闲+压卡双条件）+ 手动 /unload。"""
    hair = (ROOT / "hair_api.py").read_text(encoding="utf-8")
    _has(hair, "def _do_unload", "hair 卸载公共路径")
    _has(hair, '@app.post("/unload")', "hair 手动卸载端点")
    _has(hair, "def _idle_unload_loop", "hair 空闲自动归还线程")
    _has(hair, "HAIR_IDLE_UNLOAD_MIN", "hair 空闲阈值可调")
    _has(hair, "HAIR_KEEP_FREE_GB", "hair 压卡阈值可调（空闲机不白卸载）")
    _has(hair, "_last_used = time.time()   # 空闲自动归还的计时锚点",
         "hair_transfer 刷新使用时间")


def test_stage8_hair_style_select():
    """阶段8：开播页发型直选（与妆容样式同构）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '@app.get("/api/hair/styles")', "hub 发型样式代理")
    _has(hub, '@app.get("/api/hair/thumb")', "hub 发型缩略图代理")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "loadHairStyles", "hub.js 样式装载")
    _has(js, "hairStyleSel", "hub.js 直选状态")
    _has(js, "if (this.hairStyleSel) body.hair_style = this.hairStyleSel;",
         "hub.js 定妆脸带直选样式")
    _has(js, "else if (this.labSvc.hair && this.labSvc.hair.up) body.use_hair = true;",
         "hub.js 定妆包直选优先/激活样式兜底")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "发型样式", "ui.html 发型样式下拉")
    _has(html, "/api/hair/thumb?name=", "ui.html 选中样式缩略图")


def test_stage9_full_look():
    """阶段9：一键出片编排器（发型→妆容→试衣→微动，复用各块选择+逐步软降级）。"""
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "async runFullLook()", "hub.js 一键出片动作")
    _has(js, "fullLookSteps", "hub.js 步骤清单状态")
    _has(js, "let tryonDone = false;", "hub.js 试衣完成标记")
    _has(js, "if (!tryonDone) {", "hub.js 微动仅在无试衣底片时跑")
    _has(js, "st[k].error", "hub.js 子步失败原因透出")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "一键出片", "ui.html 出片按钮")
    _has(html, "fullLookSteps", "ui.html 步骤清单渲染")


def test_stage9_unload_logic():
    """阶段9：自动归还判定纯函数——无 GPU 单测真值表。"""
    import importlib.util
    src = (ROOT / "hair_api.py").read_text(encoding="utf-8")
    _has(src, "def _should_unload", "hair 判定纯函数存在")
    # 从源码抽函数体独立执行（不 import hair_api——避免拉起 fastapi/模型扫描）
    import re
    m = re.search(r"def _should_unload\(.*?\n(?:.+\n)+?    return .+\n", src)
    assert m, "无法抽取 _should_unload 函数体"
    ns: dict = {}
    exec(m.group(0), ns)
    f = ns["_should_unload"]
    assert f(True, 20, 5, 15, 10) is True,  "驻留+空闲久+压卡 → 卸载"
    assert f(False, 20, 5, 15, 10) is False, "未驻留 → 不动"
    assert f(True, 5, 5, 15, 10) is False,  "空闲不足 → 保留"
    assert f(True, 20, 15, 15, 10) is False, "显存充裕 → 保留（空闲机不白卸载）"
    assert f(True, 20, 5, 0, 10) is False,  "idle_min=0 → 策略停用"
    print("  [OK] _should_unload 真值表 5/5")


def test_stage10_look_history():
    """阶段10：出片历史（5 写点自动存档 + 列表/缩略图/回滚/删除 + UI 墙）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "def _look_hist_record", "hub 历史记录辅助")
    _has(hub, "LOOK_HISTORY_KEEP", "hub 每角色条数上限可调")
    _has(hub, "_LOOK_HIST_ID_RE", "hub 条目 id 防路径穿越")
    for kind in ('"hair"', '"makeup"', '"lookpack"', '"tryon"', '"idle"'):
        _has(hub, f"_look_hist_record(name, {kind}", f"hub {kind} 写点挂钩")
    _has(hub, "/api/profiles/{name}/look_history", "hub 历史列表端点")
    _has(hub, "/look_history/{hid}/image", "hub 历史图端点(thumb 缩略)")
    _has(hub, "/look_history/{hid}/restore", "hub 一键回滚端点")
    _has(hub, "/look_history/{hid}/delete", "hub 删单条端点")
    _has(hub, 'prof["face_hair_b64"] = b64img', "hub hair 回滚同步链式基底")
    _has(hub, "latest_same.get(\"md5\") == md5", "hub 连续重复去重")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "async loadLookHist", "hub.js 历史装载")
    _has(js, "restoreLookHist", "hub.js 一键回滚")
    _has(js, "deleteLookHist", "hub.js 删单条")
    _has(js, "refreshLookHistIfOpen", "hub.js 出片后墙即时刷新")
    assert js.count("refreshLookHistIfOpen()") >= 6, "6 个出片动作都应挂刷新"
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "出片历史", "ui.html 历史墙块")
    _has(html, "image?thumb=1", "ui.html 缩略图走 thumb 缓存")
    _has(html, "lookHistZoom", "ui.html 点图放大")


def test_stage11_compare_and_openapi():
    """阶段11：历史对比模式（纯前端） + openapi 500 根因修复（response_class=None）。"""
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "lookHistCmpMode", "hub.js 对比模式开关")
    _has(js, "lookHistThumbClick", "hub.js 缩略图点击分流(对比/大图)")
    _has(js, "this.lookHistCmp.shift()", "hub.js 对比槽满2张顶掉最早")
    _has(js, "lookHistById", "hub.js 对比条目反查")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "⇄ 对比", "ui.html 对比开关按钮")
    _has(html, "用这版", "ui.html 对比图一键回滚")
    _has(html, "lookHistCmp.length===1", "ui.html 单选提示")
    # openapi 修复：response_class=None（能跑但坏 schema→整站 /openapi.json 500）根除
    # 匹配带右括号的实际装饰器用法，不误伤解释性注释里的字样
    for f in ("faceswap_api.py", "hair_api.py", "tryon_api.py"):
        src = (ROOT / f).read_text(encoding="utf-8")
        assert "response_class=None)" not in src, f"{f} 仍有 response_class=None 路由"
        _has(src, 'response_class=_HTMLResp', f"{f} /ui 路由 response_class 修复")


def test_stage12_body_photo_and_arbitration():
    """阶段12：全身照记忆 + 显存仲裁（运营走查揪出的两大摩擦点）。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "def _body_photo_path", "hub 存照路径辅助")
    _has(hub, "def _save_body_photo", "hub 存照落盘(尽力而为)")
    _has(hub, "该角色还没有存照", "hub tryon_preset 无照且无存照的人话报错")
    _has(hub, '"has_body_photo": _body_photo_path(name).exists()', "hub 详情曝露存照态")
    _has(hub, "def _hair_vram_rescue", "hub 显存仲裁(卸闲置发型救试衣)")
    _has(hub, "def _is_vram_reject", "hub 显存闸拒单识别")
    assert hub.count("await _hair_vram_rescue()") >= 2, "预览+定妆两处试衣都应挂仲裁重试"
    _has(hub, "_sh.rmtree(_LOOK_HIST_ROOT / name", "hub 删角色连带清历史")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "checkFittingStored", "hub.js 存照态检查")
    _has(js, "stored:false", "hub.js fitting.stored 状态")
    _has(js, "profile: prof || ''", "hub.js 预览带 profile 走存照回退")
    _has(js, "(this.fitting.personB64 || this.fitting.stored)", "hub.js 编排器放宽试衣前置")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "换装免重传", "ui.html 存照提示")
    _has(html, "checkFittingStored()", "ui.html 切角色刷新存照态")
    assert (ROOT / "tools" / "_operator_walkthrough.py").exists(), "运营走查脚本缺失"
    ok("tools/_operator_walkthrough.py 运营走查脚本存在")


def test_stage13_effect_hints_and_walkthrough2():
    """阶段13：出片即生效提示语纠偏 + 支线走查。
    事实：body_video 内容寻址（sha1→vid_ face_id），换新视频下一句自动预计算——
    旧提示一刀切喊「重新激活」误导运营白做一步。idle_video 走 vcam 待机链，
    激活中+vcam 开启时重推即时生效。提示语必须按真实链路行为写。"""
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "def _repush_idle_if_active", "hub 激活角色待机重推(出片即生效)")
    _has(hub, "def _video_effect_hint", "hub 生效提示语按链路行为生成")
    _has(hub, "下一句口型自动换新底片", "hub body_video 激活态提示(免重新激活)")
    assert hub.count("_video_effect_hint(name,") >= 3, \
        "tryon_preset/idle_motion/历史回滚 三处都应走统一提示语"
    _has(hub, "async def _hair_transfer_call", "hub 发型显存脉冲耐心重试")
    assert hub.count("_hair_transfer_call({") >= 2, \
        "hair_preset + look_pack 两处发型调用都应走脉冲重试"
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "定妆脸已推送、口型底片下一句自动换新", "hub.js 编排器完成语区分激活态")
    assert (ROOT / "tools" / "_operator_walkthrough2.py").exists(), "支线走查脚本缺失"
    wt2 = (ROOT / "tools" / "_operator_walkthrough2.py").read_text(encoding="utf-8")
    for path in ("hair_preset", "makeup_preset", "extract_cloth", "cloth_type", "idle_motion"):
        _has(wt2, path, f"走查2覆盖 {path}")
    ok("tools/_operator_walkthrough2.py 支线走查脚本存在")


def test_stage14_catv2ton_poc():
    """阶段14：CatV2TON 视频试衣 PoC 资产完整性。
    核心資产：运行时垫片（三大版本漂移的对策）+ PoC 主脚本 + 诊断工具链 +
    实施记录结论。垫片里 RoPE 必须是 0.29 逐行复刻（不是 0.38 直调）——
    这是「衣区纯噪声」的根因修复，防回归。"""
    shim = (ROOT / "tools" / "_catv2ton_shim.py").read_text(encoding="utf-8")
    _has(shim, "pkg_resources", "垫片① pkg_resources 兼容")
    _has(shim, "read_video", "垫片② torchvision.io 视频兼容")
    _has(shim, "endpoint=False", "垫片③ RoPE 按 0.29 复刻(endpoint=False 网格)")
    _has(shim, "_rope2d_029", "垫片③ RoPE 独立实现(非 0.38 直调)")
    _has(shim, 'sys.modules["modules"]', "垫片④ modules 包骨架跳过 detectron2 链")
    poc = (ROOT / "tools" / "_catv2ton_poc.py").read_text(encoding="utf-8")
    _has(poc, "load_pose=True", "PoC 主脚本带 pose 条件(画质命门)")
    _has(poc, "DensePose", "PoC 逐帧 DensePose")
    _has(poc, "free0 < 10", "PoC 显存护栏(避让直播)")
    for tool in ("_catv2ton_x0_test.py", "_catv2ton_vae_test.py", "_catv2ton_rope_diff.py"):
        assert (ROOT / "tools" / tool).exists(), f"诊断工具 {tool} 缺失"
    ok("三件诊断工具齐全（x0/vae/rope）")
    doc = (ROOT / "妆容定妆包实施记录_20260708.md").read_text(encoding="utf-8")
    _has(doc, "CatV2TON 视频试衣 PoC 跑通", "实施记录阶段14章节")
    _has(doc, "DensePose 条件是画质命门", "实施记录记载根因链")


def test_stage15_videotryon_service():
    """阶段15：CatV2TON 动态试衣产品化（服务+Hub+UI 全链）。
    显存工程五件套是防「整机冻结」的命门，逐一防回归：
    ① 物理显存闸（WDDM 下 torch 进程视图虚高 24G，读 NVML 才是真）
    ② 动态进程配额（越界抛 OOM 不溢共享内存）+ 解码前按最新空闲重定
    ③ 顺序 CFG（去噪激活减半）④ VAE 编/解码时间分块
    ⑤ 解码期 Transformer 下卡（死重 ~3G 换 VAE 窗口余量）。"""
    svc = (ROOT / "videotryon_api.py").read_text(encoding="utf-8")
    _has(svc, "expandable_segments", "videotryon 分配器扩展段(先于 torch 导入)")
    _has(svc, "def _set_dynamic_vram_cap", "videotryon 动态显存配额(防冻机)")
    _has(svc, "set_per_process_memory_fraction", "videotryon torch 进程硬顶")
    _has(svc, "def _decode_with_headroom", "videotryon VAE 解码时间分块")
    _has(svc, 'transformer3d.to("cpu")', "videotryon 解码期 Transformer 下卡")
    _has(svc, "def _step_seq_cfg", "videotryon 顺序 CFG(去噪激活减半)")
    _has(svc, "def _slice_vae_chunked", "videotryon VAE 编码时间分块")
    _has(svc, ".float() / 255).cpu()", "videotryon smooth_video_mask 0-255→0-1 契约修正")
    _has(svc, "def _make_masker", "videotryon AutoMasker 即用即卸")
    _has(svc, "_set_dynamic_vram_cap()", "videotryon 配额在作业内生效")
    _has(svc, "phase_peaks", "videotryon 分相位峰值账本")
    _has(svc, 'delattr(_pipe, attr)', "videotryon 实例补丁 finally 清除(防双重包裹)")
    vg = (ROOT / "vram_gate.py").read_text(encoding="utf-8")
    _has(vg, "pynvml", "vram_gate 物理显存 NVML 优先")
    _has(vg, "nvidia-smi", "vram_gate nvidia-smi 兜底")
    cfg = (ROOT / "app_config.py").read_text(encoding="utf-8")
    _has(cfg, '"videotryon"', "app_config 注册 videotryon")
    _has(cfg, '"port": 8006', "videotryon 端口 8006")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, '"/api/videotryon/submit"', "hub 动态试衣提交代理")
    _has(hub, "def _videotryon_vram_rescue", "hub 显存腾挪三板斧")
    _has(hub, '_PARK_SUSPEND.add(s["service"])', "hub 被泊引擎挂起自愈(防中途回场)")
    _has(hub, "def _vtryon_unpark", "hub 作业收尾解泊")
    _has(hub, "async def api_engine_stop(name: str, suspend: int = 0)",
         "engine/stop 支持 suspend(腾挪场景防自愈拉回)")
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "runVideoTryon", "hub.js 动态试衣提交+轮询")
    _has(js, "applyVideoTryon", "hub.js 应用为待机视频")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "动态试衣(视频)", "ui.html 动态试衣按钮")
    _has(html, "vtryon.progress", "ui.html 作业进度条")
    if (ROOT / "start_videotryon_api.bat").exists():
        ok("start_videotryon_api.bat 启动器存在")
    else:
        ng("start_videotryon_api.bat 缺失")
    bat = (ROOT / "start_all_services.bat").read_text(encoding="utf-8", errors="replace")
    _has(bat, "start_videotryon_api.bat", "start_all_services 扩展组启动 videotryon")
    if (ROOT / "tools" / "_videotryon_smoke.py").exists():
        ok("冒烟脚本 _videotryon_smoke.py 存在")
    else:
        ng("tools/_videotryon_smoke.py 缺失")
    doc = (ROOT / "妆容定妆包实施记录_20260708.md").read_text(encoding="utf-8")
    _has(doc, "阶段 15 实施", "实施记录阶段15章节")


def test_stage5_vram_gate_module():
    """阶段5：显存准入闸公共模块 + hair/tryon 双服务接入。"""
    vg = (ROOT / "vram_gate.py").read_text(encoding="utf-8")
    _has(vg, "def gate(", "vram_gate.gate 入口")
    _has(vg, "VRAM_GATE_OFF", "vram_gate 应急逃生阀")
    _has(vg, "mem_get_info", "vram_gate 真实显存查询")
    hair = (ROOT / "hair_api.py").read_text(encoding="utf-8")
    _has(hair, "vram_gate.gate", "hair_api 接入准入闸")
    _has(hair, "HAIR_MIN_FREE_GB", "hair_api 阈值可调")
    ta = (ROOT / "tryon_api.py").read_text(encoding="utf-8")
    _has(ta, "import vram_gate as _vgate", "tryon_api 复用公共闸")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "生成待机微动", "ui.html 待机微动按钮")


def test_ui_wiring():
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    _has(js, "runMakeupPreset", "hub.js 妆容定妆动作")
    _has(js, "runLookPack", "hub.js 一键定妆包动作")
    _has(js, "loadMakeupStyles", "hub.js 妆容样式装载")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "一键定妆包", "ui.html 定妆包按钮")
    _has(html, "妆容样式", "ui.html 妆容样式选择")


def test_launcher():
    bat = (ROOT / "start_all_services.bat").read_text(encoding="utf-8", errors="replace")
    _has(bat, "makeup_api.py", "start_all_services 扩展组启动 makeup")
    if (ROOT / "start_makeup_api.bat").exists():
        ok("start_makeup_api.bat 独立启动器存在")
    else:
        ng("start_makeup_api.bat 缺失")


def test_stage2_color_sync():
    """阶段2：定妆↔直播妆容 色彩单一真相联动。"""
    mk = (ROOT / "makeup_api.py").read_text(encoding="utf-8")
    _has(mk, '"detail": PRESETS', "makeup_api styles 返回预设色板 detail")
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "def _live_makeup_suggest", "hub 定妆成功回写 live_makeup 建议色")
    _has(hub, "_live_makeup_suggest(_profiles[name], _mk_applied)", "hub look_pack 链路接入建议色")


def test_stage2_live_makeup_ui():
    js = (ROOT / "static" / "hub.js").read_text(encoding="utf-8")
    for needle, label in [("saveLiveMakeup", "保存直播妆容"),
                          ("loadLiveMakeup", "装载角色 live_makeup"),
                          ("fillLiveMakeupFromStyle", "从预设取色"),
                          ("hexToBgr", "hex→BGR 变换"),
                          ("bgrToHex", "BGR→hex 变换")]:
        _has(js, needle, f"hub.js {label}")
    html = (ROOT / "static" / "ui.html").read_text(encoding="utf-8")
    _has(html, "直播妆容层", "ui.html 直播妆容面板")
    _has(html, "liveMakeup.lipS", "ui.html 口红强度滑杆")


def test_stage2_tryon_animate():
    hub = (ROOT / "avatar_hub.py").read_text(encoding="utf-8")
    _has(hub, "def _tryon_animate_ditto", "hub 试衣→Ditto 待机动化")
    _has(hub, "def _silence_wav", "hub 静音 WAV 生成（Ditto 静默驱动）")
    _has(hub, '"animated": animated', "hub tryon_preset 汇报动化结果")


def test_stage2_activation_script():
    ps = ROOT / "tools" / "apply_look_pack_update.ps1"
    if not ps.exists():
        ng("tools/apply_look_pack_update.ps1 缺失")
        return
    src = ps.read_text(encoding="utf-8")
    for needle, label in [("realtime_status.json", "直播避让闸"),
                          ("py_compile", "编译闸门"),
                          ("bak_lookpack", "远端备份回滚"),
                          ("_verify_104_makeup2", "makeup 功能级验证(openapi 500 后的替代)"),
                          ("_lookpack_smoke", "临时角色冒烟")]:
        _has(src, needle, f"激活脚本 {label}")
    if (ROOT / "激活定妆包.bat").exists():
        ok("激活定妆包.bat 双击入口存在")
    else:
        ng("激活定妆包.bat 缺失")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n== {t.__name__} ==")
        try:
            t()
        except Exception as e:
            ng(f"{t.__name__} 异常: {e}")
    print("\n" + ("=" * 46))
    if FAIL:
        print(f"[GATE] {len(FAIL)} 项未过：")
        for f in FAIL:
            print("  - " + f)
        sys.exit(1)
    print(f"[GATE] Look Pack 门禁全部通过（{len(tests)} 组）")
