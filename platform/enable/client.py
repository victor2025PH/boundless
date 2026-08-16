# -*- coding: utf-8 -*-
"""platform/enable/client.py — 赋能能力网关『瘦客户端』(纯 stdlib，可降级)。

见 CONTRACT.md：三项赋能能力都在底层引擎**就地**完成(人设克隆音 TTS / 数字人渲染在 avatarhub，
翻译在 chengjie 翻译栈)；本客户端只消费其 HTTP 能力面。引擎不在线时**不抛异常**，
返回 {"available": False,...}，让承接中台(chengjie)在生成 AI 回复时能安全地『弱依赖』赋能——
调用方的优雅退化约定：
- tts_clone_speak 不可用 → 退化为发纯文本回复；
- translate 不可用 → 直接发原文(或提示暂不支持翻译)；
- avatar_render 不可用 → 只发音频/文本，不发数字人视频。

依赖铁律：只用 stdlib(urllib/json/os/typing)，不 import engines/products/website，也不 import 第三方包。

隐私红线：translate 的原文只作为本次请求载荷发往 chengjie 翻译栈，本客户端不落任何日志/事件；
计量只用字符计数(见 enable_schema.json)。

【2026-07-19 融合期第三阶段·契约纠偏】首版契约在真正读到 avatarhub 源码前，臆测了
`/api/voice_clone/tts`(URL 返回)与 `/api/avatar/render` 两个端点名与响应形状。对照
`avatar_hub.py` 源码核实后：真实 TTS 端点是已在跑的 `/api/tts_only`（入参 `profile` 非
`profile_id`；响应 `audio_base64` 内联音频，非 `audioUrl`）；`/api/enable/status` 已按本
契约原样在 avatarhub 落地(纯探针，见 CONTRACT.md)。avatar_render 对应的真实前缀是
`/avatar/speak`（"数字人开口"族），但其请求/响应字段尚未逐一核实，**本版本先只把路径
改对，字段形状标注"待第四阶段验证"，不假装已完全对齐**——错的契约比没有契约更危险。

【2026-07-19 融合期第四阶段·avatar_render 字段核实】逐行读完 `/avatar/speak`
(`SpeakRequest`/`SpeakResponse`/`avatar_speak` handler)源码，并用文件内嵌前端 JS
(`/ui`、`/mobile/ui`)与内部调用方 `_station_announce`(点歌播报)的真实调用样例交叉
验证：真实字段与"待验证"时的猜测出入很大——没有 `audio_url` 输入(`text` 恒必填，
这个端点是"文本进→现场TTS→可选联动口型"一体流程，不支持喂入已合成音频)；`persona_id`
应对齐为 `profile`(与 `tts_clone_speak` 同一个 `_profiles` 档案，非另一套身份体系)；
视频(`lipsync_video_b64`)是要显式 `generate_lipsync=true` **且**该角色已用
`PATCH /profiles/{name}` 配过 `face_b64` 才会非空的软依赖，不是保证产物，缺前置装配
时静默退化回纯音频、不报错；响应体音频/视频全部是内联 base64，没有 `videoUrl`。已按
真实字段重写 `avatar_render()`；同族的流式(`/avatar/speak/stream`)与无口型能力的批量
(`/avatar/speak/batch(/stream)`)变体不纳入本契约，理由见该方法 docstring 与
CONTRACT.md §6。

用法：
    from client import EnableClient
    ec = EnableClient()                       # 两个 base_url 分别读 AVATARHUB_BASE_URL / CHENGJIE_BASE_URL
    if ec.available():
        r = ec.tts_clone_speak("你好", profile_id="p_001")   # {"audio_base64":..., "elapsed_ms":..., "ok":...}
    st = ec.status()                          # 始终安全：不可达时 {"available": False, ...}
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

_DEFAULT_AVATARHUB = os.environ.get("AVATARHUB_BASE_URL", "http://127.0.0.1:9000")
_DEFAULT_CHENGJIE = os.environ.get("CHENGJIE_BASE_URL", "http://127.0.0.1:8080")


class EnableClient:
    def __init__(self, avatarhub_url: Optional[str] = None,
                 chengjie_url: Optional[str] = None, timeout: float = 8.0,
                 chengjie_token: Optional[str] = None,
                 avatarhub_token: Optional[str] = None):
        self.avatarhub_url = (avatarhub_url or _DEFAULT_AVATARHUB).rstrip("/")
        self.chengjie_url = (chengjie_url or _DEFAULT_CHENGJIE).rstrip("/")
        self.timeout = timeout
        # 2026-07-19 追加：chengjie /api/translate·/api/enable/status 走同一套
        # _api_auth（支持 Bearer）；配了 web_admin.auth_token 时无头会一律 401。
        self.chengjie_token = (chengjie_token if chengjie_token is not None
                               else os.environ.get("CHENGJIE_AUTH_TOKEN", "").strip() or None)
        # 2026-07-20 第五阶段追加：avatarhub 的 /api/tts_only、/avatar/speak 等写操作
        # 走**不同的鉴权头**——`X-AH-Token`（裸令牌，非 `Authorization: Bearer`），
        # 见 avatar_hub.py `_auth_middleware`：`request.headers.get("X-AH-Token") or
        # request.cookies.get("ah_token") or request.query_params.get("ah_token")`。
        # 本客户端只用头（不用 cookie/query，避免令牌出现在日志/URL 里）。
        # `_API_TOKEN` 未配置时 avatarhub 中间件直接放行，此时不传该头也无影响
        # （向后兼容：不配置 avatarhub_token 时行为与之前完全一致）。
        self.avatarhub_token = (avatarhub_token if avatarhub_token is not None
                                else os.environ.get("AVATARHUB_AUTH_TOKEN", "").strip() or None)

    # ---- 内部 HTTP（可降级：任何失败都收敛为 dict，不抛给调用方）----
    def _get(self, base: str, path: str, *, bearer: Optional[str] = None,
             ah_token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(base, "GET", path, None, bearer=bearer, ah_token=ah_token)

    def _post(self, base: str, path: str, payload: Dict[str, Any], *,
              bearer: Optional[str] = None, ah_token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(base, "POST", path, payload, bearer=bearer, ah_token=ah_token)

    def _request(self, base: str, method: str, path: str,
                 payload: Optional[Dict[str, Any]], *,
                 bearer: Optional[str] = None, ah_token: Optional[str] = None) -> Dict[str, Any]:
        url = base + path
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if ah_token:
            headers["X-AH-Token"] = ah_token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                out = json.loads(body) if body else {}
                if isinstance(out, dict):
                    out.setdefault("available", True)
                    return out
                return {"available": True, "data": out}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return {"available": False, "error": f"HTTP {e.code}", "detail": detail}
        except Exception as e:  # 连接失败/超时/JSON 解析失败 —— 一律降级
            return {"available": False, "error": str(e)[:200]}

    # ---- 契约方法（见 CONTRACT.md §2）----
    def tts_clone_speak(self, text: str, profile_id: str,
                        lang: str = "zh", fmt: str = "ogg") -> Dict[str, Any]:
        """/api/tts_only(avatarhub，真实已在跑端点) —— 把回复文本合成为人设克隆音。

        字段对照 avatarhub `TtsOnlyRequest`：`profile_id` 映射到其 `profile` 字段
        (角色档案键，非文件路径)；`lang`/`fmt` 目前未逐一对齐(该端点用 `language`/
        `emotion`/`fish_params`，先不传，用其默认值，避免猜错参数名导致 400)。

        成功返回 {"ok":True, "audio_base64":..., "profile":..., "elapsed_ms":..., "available":True}
        —— 音频是**内联 base64**，不是 URL(旧版契约猜测有误，已按源码核实纠正)。
        `ok=False`(如角色不存在/合成失败)与 HTTP 层不可达都会让 `available` 或 `ok` 为假，
        调用方统一按"不可用"退化为发纯文本回复。
        """
        return self._post(self.avatarhub_url, "/api/tts_only",
                          {"profile": profile_id, "text": text},
                          ah_token=self.avatarhub_token)

    def translate(self, text: str, to_lang: str,
                  from_lang: Optional[str] = None) -> Dict[str, Any]:
        """/api/translate(chengjie 翻译栈) —— 翻译回复文本。

        成功返回含 text(译文)/detected_lang；不可用返回 {"available": False,...}，
        调用方应退化为直接发原文。原文不落任何日志/事件，计量只用字符计数。
        """
        payload: Dict[str, Any] = {"text": text, "to_lang": to_lang}
        if from_lang is not None:
            payload["from_lang"] = from_lang
        return self._post(self.chengjie_url, "/api/translate", payload, bearer=self.chengjie_token)

    def translate_status(self) -> Dict[str, Any]:
        """GET /api/enable/status(chengjie，2026-07-19 第三阶段新增) —— 翻译能力就绪探针。

        与 status()/available() 探测的 avatarhub 赋能面是**不同的服务**(不同 host:port)，
        字段也不同(chengjie 返回 translate_ready，见 CONTRACT.md)；调用方若只关心
        "translate 能不能用"，应看这个而不是 status()。
        """
        return self._get(self.chengjie_url, "/api/enable/status", bearer=self.chengjie_token)

    def avatar_render(self, text: str, profile_id: str = "",
                      generate_lipsync: bool = True) -> Dict[str, Any]:
        """/avatar/speak(avatarhub，真实已在跑端点，"数字人开口"族的**基础/非流式**
        变体) —— 文本现场合成语音，并可联动出一段口型同步视频。

        【2026-07-19 第四阶段核实：路径与字段已逐行核对源码(`SpeakRequest`/
        `SpeakResponse`/`avatar_speak` handler)，并用文件内嵌前端 JS(`/ui`、
        `/mobile/ui`)与内部调用方 `_station_announce`(点歌播报)的真实调用样例交叉
        验证。结论：与首版契约的设想有本质差异，逐条纠正如下】

        1. 无 `audio_url` 输入——`SpeakRequest.text` 无默认值恒为必填，这个端点是
           "文本→现场TTS→可选联动口型"一体流程，不支持喂入已合成音频跳过 TTS；首版
           "audio_url 与 text 二选一"的设想在源码中不存在，本版直接去掉该参数。
        2. `persona_id` 改名对齐为 `profile_id`——真实字段是 `profile`(空串=用服务端
           当前激活角色)，与 `tts_clone_speak` 是**同一个 `_profiles` 档案**(旁证：
           创建档案时打的埋点事件正是 `huanying.persona.created`——"persona"在这个
           代码库里就是"profile"的业务叫法，不是另一套身份体系)。
        3. 视频是可选且有前置条件的软依赖，不是保证产物——请求字段 `generate_lipsync`
           服务端默认 `False`(本方法出于"叫 avatar_render 就是为了要视频"把默认改为
           `True`；只要音频请直接用 `tts_clone_speak`)。即便传 `True`，还需要该
           `profile` 已经用 `PATCH /profiles/{name}` 配过 `face_b64`(人脸底图，本
           客户端未提供配置该字段的方法)，否则**静默**退化回纯音频、不报错、
           `available` 仍是 True——判断"是否真拿到视频"必须看返回的
           `lipsync_video_b64` 是否非空，不能只看 HTTP 是否成功。avatarhub 自己的
           内部调用方 `_station_announce` 也是"先看 `lipsync_video_b64` 非空再用，
           空则退化播纯音频"，本方法与该内部约定一致。
        4. 响应字段是内联 base64，不是 URL——`audio_base64`(必有)/`lipsync_video_b64`
           (可选，base64 MP4)，均非首版契约猜测的 `videoUrl`；`elapsed_ms` 是本次
           请求的处理耗时，不是"视频时长"(首版对 `ms` 语义的猜测有误，已纠正)。
        5. 同样受商业化授权闸门管辖——`/avatar/speak` 前缀在 `_GEN_BLOCK_PREFIX` 里
           (与 `tts_clone_speak` 命中的 `_GEN_BLOCK_EXACT` 是同一套
           `_license_gate_middleware`)，试用/授权到期且强制模式时会先被 403
           `{"ok":false,"error":"license_expired"}` 挡下；鉴权头同 `tts_clone_speak`
           一致——非回环来源需 `X-AH-Token`。
        6. 只封装这一个基础同步变体——同族的 `/avatar/speak/stream`、
           `/avatar/speak/batch/stream` 返回 SSE(`StreamingResponse`)进度流而非
           一次性 JSON，`/avatar/speak/batch` 虽是 JSON 但入参是裸 `List[...]`且
           **无口型/视频能力**(仅批量TTS)；三者都不符合本瘦客户端"一次 POST 拿完整
           结果"的设计前提，均不纳入本方法，见 CONTRACT.md §6。

        成功返回 {"available":True, "audio_base64":..., "lipsync_video_b64":...,
        "elapsed_ms":..., "face_image":"", "rvc_applied":bool, "warning":"",
        "detected_emotion":""} —— 无 `ok` 字段(这点与 `tts_only` 不同)。`profile_id`
        对应角色不存在时不报错，静默退化为默认音色/无口型(与 `tts_clone_speak` 的
        "角色不存在"容错行为一致)。
        """
        payload: Dict[str, Any] = {"text": text, "generate_lipsync": bool(generate_lipsync)}
        if profile_id:
            payload["profile"] = profile_id
        return self._post(self.avatarhub_url, "/avatar/speak", payload,
                          ah_token=self.avatarhub_token)

    def status(self) -> Dict[str, Any]:
        """/api/enable/status(avatarhub) —— 赋能面可达性探针(能力开关/负载)。

        当前该 GET 端点不在 avatarhub `_AUTH_SENSITIVE_GET` 白名单内，本不需要令牌；
        仍顺带带上 `avatarhub_token`（若配置），对不需要鉴权的场景无副作用，同时
        对"以后被加进敏感 GET 名单"这种情况提前兜底。
        """
        return self._get(self.avatarhub_url, "/api/enable/status", ah_token=self.avatarhub_token)

    def available(self) -> bool:
        """avatarhub 赋能面是否就绪(HTTP 可达)。translate 走 chengjie，以其返回的 available 单独判断。"""
        st = self.status()
        return bool(st.get("available"))


def _selftest() -> int:
    ec = EnableClient()
    print(f"[enable.client] avatarhub_url={ec.avatarhub_url}")
    print(f"[enable.client] chengjie_url={ec.chengjie_url}")
    st = ec.status()
    print(f"  status(): available={st.get('available')} note={str(st.get('error',''))[:60]}")
    print(f"  available()={ec.available()}  (引擎未在线属正常，客户端已降级不抛错)")
    return 0


if __name__ == "__main__":
    import sys
    # Windows 下 stdout 默认本地代码页(cp936 等)打中文会乱码，统一 UTF-8（与 leadbus/observability 一致）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    sys.exit(_selftest())
