"""
配置管理器
负责加载、验证和管理配置文件
"""

import yaml
import os
from pathlib import Path
from typing import Dict, Any, Optional
import logging


def _path_str(p: Any) -> str:
    """日志用路径字符串（Path→posix，其余→str）。

    绝不抛异常：这些调用点都在「可选文件缺失」的降级分支里，日志格式化把
    整个请求弄崩过一次（安装版缺 config/templates.yaml → dashboard 500）。
    """
    as_posix = getattr(p, "as_posix", None)
    return as_posix() if callable(as_posix) else str(p)


def _nested_patch(keys, value) -> Dict[str, Any]:
    """['a','b','c'], v → {'a': {'b': {'c': v}}}（内存深合并用的最小 patch）。"""
    out: Dict[str, Any] = {}
    node = out
    for k in keys[:-1]:
        node[k] = {}
        node = node[k]
    node[keys[-1]] = value
    return out


def merge_yaml_patch_preserving(p: Path, patch: Dict[str, Any], replace: set,
                                merge_fn) -> bool:
    """P3 2026-08-01：对 YAML 文件做 replace 感知的 patch 深合并，**保留注释**。

    ``merge_fn(dst, patch, replace)``＝调用方的合并算法（ConfigManager.
    ``_merge_patch_replace_aware``，对 CommentedMap 同样成立——它只用 dict 接口）。
    任何一步不满足 → False，调用方回落旧 yaml.dump 路径（写入永不丢失）。
    """
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.comments import CommentedMap
    except Exception:
        return False
    try:
        if not isinstance(patch, dict) or not patch:
            return False
        y = YAML()
        y.preserve_quotes = True
        y.width = 4096
        data = None
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = y.load(f)
        if data is None:
            data = CommentedMap()
        if not isinstance(data, dict):
            return False
        merge_fn(data, patch, replace)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            y.dump(data, f)
        os.replace(tmp, p)
        return True
    except Exception:
        logging.getLogger("ai_chat_assistant.ConfigManager").debug(
            "merge_yaml_patch_preserving 失败（回落旧路径）", exc_info=True)
        return False


def set_yaml_key_preserving(p: Path, keys, value) -> bool:
    """P2 2026-08-01：对 YAML 文件做单键深写，**保留全部注释/引号/顺序**。

    动机（当日实锤）：`set_overlay_flag` 旧实现用 `yaml.safe_load` + `yaml.dump`
    整文件重写——config.local.yaml 里约 30 行人工运维注释被一次 enable_dry 全部
    剃光。本函数走 ruamel.yaml round-trip（本机已装 0.18.x），任何一步不满足
    （无 ruamel / 文件根不是映射 / 解析失败）→ 返回 False，调用方回落旧路径，
    **绝不因保注释尝试而丢一次写入**。纯函数级（除文件 I/O），可直接单测。
    """
    try:
        from ruamel.yaml import YAML
    except Exception:
        return False
    try:
        keys = [str(k) for k in (keys or []) if str(k)]
        if not keys:
            return False
        y = YAML()
        y.preserve_quotes = True
        y.width = 4096
        data = None
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = y.load(f)
        if data is None:
            from ruamel.yaml.comments import CommentedMap
            data = CommentedMap()
        if not isinstance(data, dict):
            return False
        node = data
        for k in keys[:-1]:
            nxt = node.get(k)
            if not isinstance(nxt, dict):
                from ruamel.yaml.comments import CommentedMap
                nxt = CommentedMap()
                node[k] = nxt
            node = nxt
        node[keys[-1]] = value
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            y.dump(data, f)
        tmp.replace(p)
        return True
    except Exception:
        logging.getLogger("ai_chat_assistant.ConfigManager").debug(
            "set_yaml_key_preserving 失败（回落旧路径）", exc_info=True)
        return False


class ConfigManager:
    """配置管理器类"""

    def __init__(self, config_path: str = None):
        """
        初始化配置管理器

        Args:
            config_path: 配置文件路径，如果为None则使用默认路径
        """
        # logger 必须先于 _get_default_config_path()，后者在回退到 example 配置时
        # 会用 self.logger.warning（纯净 checkout 无 config.yaml 时即触发）。
        # P2-198（2026-07-31 探针实锤）：logger 名必须挂进 ai_chat_assistant 树——
        # 旧名 `src.utils.config_manager` 无 handler 也不属于该树，INFO（含
        # 「配置热重载完成」）全部静默丢弃、WARNING 经 logging.lastResort 漏进
        # stderr（198 的 err 日志乱码行就是它）。热重载**功能一直是好的**，
        # 只是完全不可见——排障时被误判成「热重载没跑」，浪费一轮复现。
        self.logger = logging.getLogger("ai_chat_assistant.ConfigManager")
        # WP-1：本次进程是否刚做过 config 全新播种（_ensure_seeded 真拷贝）。
        # 必须在 config_path 解析**之前**置默认——解析过程可能把它翻 True。
        # 它是 _ensure_deploy_profile 的闸门之一：预设档只应用在「全新安装」，
        # 升级安装/既有配置的机器（含生产双实例）永不被触碰。
        self._seeded_fresh = False
        self.config_path = Path(config_path) if config_path else self._get_default_config_path()
        self.config: Dict[str, Any] = {}
        self._quota_rules_cache: Optional[Dict[str, Any]] = None
        self._quota_rules_mtime: float = 0
        self._templates_cache: Optional[Dict[str, Any]] = None
        self._templates_mtime: float = 0
        self._exchange_rates_cache: Optional[Dict[str, Any]] = None
        self._exchange_rates_mtime: float = 0
        self._strategies_cache: Optional[Dict[str, Any]] = None
        self._strategies_mtime: float = 0
        self._config_mtime: float = 0
        self._overlay_loaded_mtime: float = 0  # config.local.yaml 装载基线（热重载双文件监视）
        self._hot_reload_interval: float = 30.0
        self._last_hot_reload_check: float = 0
        self._on_reload_callbacks: list = []

    def _get_default_config_path(self) -> Path:
        """获取默认配置文件路径。

        解析优先级：
        1. ``AITR_CONFIG_PATH`` 环境变量（显式指向 config.yaml）——打包/自包含部署用，
           桌面端把它指向用户**可写目录**，避免写进只读的安装包。
        2. ``AITR_DATA_DIR`` 环境变量（数据根）——config = ``$AITR_DATA_DIR/config/config.yaml``。
        3. 仓库默认 ``<repo>/config/config.yaml``（开发态，行为不变）。

        命中 1/2 时若目标不存在，会从**内置 example** 自播种到该可写路径（见 ``_ensure_seeded``），
        使首次运行即有可编辑的 config，无需 launcher 额外搬运。
        """
        env_path = os.environ.get("AITR_CONFIG_PATH")
        if env_path:
            target = Path(env_path).expanduser()
            self._ensure_seeded(target)
            self._ensure_seeded_extras(target)
            return target

        env_dir = os.environ.get("AITR_DATA_DIR")
        if env_dir:
            target = Path(env_dir).expanduser() / "config" / "config.yaml"
            self._ensure_seeded(target)
            self._ensure_seeded_extras(target)
            return target

        # 优先使用仓库 config/config.yaml
        current_dir = Path(__file__).parent.parent.parent
        config_file = current_dir / "config" / "config.yaml"

        # 如果不存在，使用config.example.yaml
        if not config_file.exists():
            example_file = current_dir / "config" / "config.example.yaml"
            if example_file.exists():
                self.logger.warning(f"配置文件 {config_file} 不存在，请复制 {example_file} 并编辑")
                return example_file

        return config_file

    def _bundled_example_path(self) -> Optional[Path]:
        """内置 example 配置路径（开发态=仓库 config/；打包态=冻结资源内 config/）。

        PyInstaller onedir 下 ``__file__`` 落在 ``_internal/src/utils/...``，故
        parent.parent.parent 即 ``_internal``，与 ``--add-data config/...`` 落点一致。
        """
        try:
            base = Path(__file__).resolve().parent.parent.parent
            ex = base / "config" / "config.example.yaml"
            return ex if ex.exists() else None
        except Exception:
            return None

    def _bundled_desktop_min_path(self) -> Optional[Path]:
        """内置桌面最小种子配置路径（P0-1 A1，落点同 example）。

        无 ``YOUR_*`` 占位、只含「翻译最短路径」必需键；``AITR_DESKTOP_MODE`` 下播种
        优先于完整 example，使干净机首启不带占位脏值（AI Key 由首启向导写 overlay）。
        """
        try:
            base = Path(__file__).resolve().parent.parent.parent
            mn = base / "config" / "config.desktop.min.yaml"
            return mn if mn.exists() else None
        except Exception:
            return None

    def _ensure_seeded(self, target: Path) -> None:
        """打包/自包含部署：目标 config 不存在时，从内置种子播种到用户可写目录。

        桌面模式（``AITR_DESKTOP_MODE`` 真）优先播种最小桌面种子（无占位），
        否则回落完整 example。失败永不抛（仅告警）；无内置种子时不创建
        （load() 会报缺失并提示）。
        """
        try:
            if target.exists():
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            ex: Optional[Path] = None
            if self._env_truthy("AITR_DESKTOP_MODE"):
                ex = self._bundled_desktop_min_path()
            if not ex:
                ex = self._bundled_example_path()
            if ex and ex.exists():
                import shutil
                shutil.copyfile(str(ex), str(target))
                self._seeded_fresh = True   # 全新安装标记（deploy profile 播种闸门）
                self.logger.warning("配置不存在，已从内置种子播种到可写目录: %s ← %s", target, ex.name)
            else:
                self.logger.warning("配置不存在且无内置 example，可写目录仍缺 config: %s", target)
        except Exception as exc:
            self.logger.warning("配置播种失败（忽略）: %s", exc)

    # ── 随包数据种子（内测/定制包：人设/语音/相册/KB + 功能 overlay）──────────
    # 内测包（v1.001 起）把生产机资产暂存进 resources/seed-data/（见
    # desktop/build/stage_internal_assets.py），首启播种到用户数据区。标准包没有
    # 该目录、服务器部署没有 AITR_SEED_DATA_DIR → 整条链 no-op，行为零变化。
    # ⚠ config/knowledge_base.db 已从清单移除（J-9 #184，2026-09-05）：即使旧种子目录里
    # 还躺着生产 KB 快照，首启也不再拷进用户数据区——厂商产品话术不进用户知识库。
    _SEED_ASSET_ITEMS = (
        "config/profiles_runtime.yaml",
        "config/voice_refs",
        "config/prerender_lines",
        "config/persona_albums",
        "config/persona_bio.db",
        "config/persona_media.db",
        "assets/voices",
    )

    def _seed_extras_dir(self) -> Optional[Path]:
        """随包种子目录：env 显式指定优先；冻结产物自动发现 resources/seed-data。"""
        env = (os.environ.get("AITR_SEED_DATA_DIR") or "").strip()
        if env:
            p = Path(env).expanduser()
            return p if p.is_dir() else None
        try:
            import sys
            if getattr(sys, "frozen", False):
                # resources/backend/backend.exe → resources/seed-data
                p = Path(sys.executable).resolve().parent.parent / "seed-data"
                return p if p.is_dir() else None
        except Exception:
            pass
        return None

    def _ensure_seeded_extras(self, target: Path) -> None:
        """把随包种子（若有）播种进用户数据区——覆盖「全新安装」与「升级安装」两态。

        与 ``_ensure_seeded``（config 主文件）同哲学，但每一项独立「缺才补」：
        目标已存在的文件/目录一概不动（用户改过的人设、追加的参考音不会被覆盖）；
        功能 overlay 已存在时只补**缺失键**（显式 true/false 都尊重——与
        ``_ensure_baseline`` 同一条三态语义）。无种子目录 = 全程 no-op。永不抛。
        """
        try:
            seed = self._seed_extras_dir()
            if not seed:
                return
            import shutil
            config_dir = target.parent
            data_root = config_dir.parent
            self._seed_overlay_from(
                seed / "config.local.internal.yaml",
                config_dir / "config.local.yaml")
            for rel in self._SEED_ASSET_ITEMS:
                src = seed / rel
                dst = data_root / rel
                try:
                    if not src.exists() or dst.exists():
                        continue
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if src.is_dir():
                        shutil.copytree(str(src), str(dst))
                    else:
                        shutil.copyfile(str(src), str(dst))
                    if dst.name == "persona_media.db":
                        self._absolutize_media_db(dst, data_root)
                    self.logger.warning("已播种随包资产: %s", rel)
                except Exception as exc:
                    self.logger.warning("随包资产播种失败（忽略 %s）: %s", rel, exc)
        except Exception as exc:
            self.logger.warning("随包资产播种异常（忽略）: %s", exc)

    def _seed_overlay_from(self, src: Path, dst: Path) -> None:
        """功能 overlay 种子 → config.local.yaml：缺则整份拷；有则只补缺失键。"""
        try:
            if not src.exists():
                return
            import shutil
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(src), str(dst))
                self.logger.warning(
                    "已播种功能 overlay: %s ← %s", dst, src.name)
                return
            with open(src, "r", encoding="utf-8") as f:
                seed = yaml.safe_load(f) or {}
            with open(dst, "r", encoding="utf-8") as f:
                cur = yaml.safe_load(f) or {}
            if not isinstance(seed, dict) or not isinstance(cur, dict):
                return
            added = self._merge_missing(cur, seed)
            if not added:
                return
            tmp = dst.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 运行时设置 + 随包种子补齐的 overlay（深合并覆盖 config.yaml）。\n")
                yaml.safe_dump(cur, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, dst)
            self.logger.warning("功能 overlay 已按种子补齐 %d 个缺失键", added)
        except Exception as exc:
            self.logger.warning("功能 overlay 播种失败（忽略）: %s", exc)

    @staticmethod
    def _merge_missing(base: Dict[str, Any], seed: Dict[str, Any]) -> int:
        """把 seed 里 base 缺失的键补进 base（就地）；已有键（含显式 false/None）不动。

        返回补入的键数（叶/子树都按 1 计）。与 ``_deep_merge`` 的区别＝方向相反：
        _deep_merge 用 over 覆盖，本方法只填空位。
        """
        added = 0
        for k, v in (seed or {}).items():
            if k not in base:
                base[k] = v
                added += 1
            elif isinstance(v, dict) and isinstance(base.get(k), dict):
                added += ConfigManager._merge_missing(base[k], v)
        return added

    @staticmethod
    def _absolutize_media_db(db: Path, data_root: Path) -> None:
        """相册注册表 file_path：种子=「相对数据根」→ 落地改写为本机绝对路径。

        生产实例登记的就是绝对路径（viewer/发送链不依赖 CWD）；种子为了跨机分发
        在暂存时归一成相对（见 stage_internal_assets._rewrite_media_db），播种落地
        这一刻才知道目标数据根，在此收口。
        """
        import sqlite3
        con = sqlite3.connect(str(db))
        try:
            rows = con.execute(
                "SELECT id, file_path FROM persona_media").fetchall()
            for mid, fp in rows:
                p = str(fp or "")
                if not p or Path(p).is_absolute():
                    continue
                con.execute(
                    "UPDATE persona_media SET file_path = ? WHERE id = ?",
                    (str((data_root / p).resolve()), mid))
            con.commit()
        finally:
            con.close()

    async def load(self) -> bool:
        """加载配置文件"""
        try:
            if not self.config_path.exists():
                self.logger.error(f"配置文件不存在: {self.config_path}")
                return False

            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}

            # P1-1：凭证 overlay（config.local.yaml）深合并覆盖在主配置之上。
            # 接入向导只写这个小文件 → 主 config.yaml 的注释/结构永不被改写，
            # 且密钥与 git 跟踪文件分离（overlay 应进 .gitignore）。
            self._merge_overlay()

            # 桌面态产品基线增量补齐（feature_registry A 类）。必须在
            # _validate_config **之前**：桌面首启 telegram/ai 凭据为空时本方法
            # 下方会 validation-fail 提前 return False（main.py 忽略返回值继续跑），
            # 挂在成功路径末尾等于桌面态永不执行。
            self._ensure_baseline()

            # WP-1：部署能力预设档首启播种（AITR_DEPLOY_PROFILE + 全新播种双闸；
            # 同样必须在 _validate_config 之前——理由同上）。
            self._ensure_deploy_profile()

            # 打包/自包含部署：用 AITR_WEB_* 覆盖 web_admin.{host,port,auth_token}，
            # 使后端「serve 的端口/令牌」与桌面壳 renderer「talk 的 base_url/token」强一致，
            # 无需改随包 example（server 端口/令牌保持 canonical）。开发/server 态无 env→零影响。
            self._apply_env_overrides()

            # 验证配置
            if not self._validate_config():
                return False

            self._config_mtime = os.path.getmtime(self.config_path)
            self._overlay_loaded_mtime = self._overlay_mtime()  # 双文件监视基线
            self.logger.info(f"配置文件加载成功: {self.config_path}")
            # P0-1：非阻断启动自检 — 把 error/warn 摘要打到日志，引导修复错配，
            # 但不改变启动成败（严格 gate 走 `python main.py --check`）。
            self._run_startup_self_check()
            return True

        except yaml.YAMLError as e:
            self.logger.error(f"配置文件YAML格式错误: {e}")
            return False
        except Exception as e:
            self.logger.error(f"加载配置文件失败: {e}")
            return False

    @staticmethod
    def _env_truthy(name: str) -> bool:
        return str(os.environ.get(name) or "").strip().lower() in (
            "1", "true", "yes", "on")

    def _apply_env_overrides(self) -> None:
        """以环境变量覆盖运行时配置（仅当设置时；不写回磁盘）。

        - ``AITR_WEB_*``：桌面壳 launcher 注入，保证后端 serve 与 renderer talk 一致。
        - ``AITR_DESKTOP_MODE``：强制 ``web_admin.enabled=true``。
        - ``AITR_HOSTED_AI_*``：厂商托管试用 Key（打包/实例注入，**永不入库**）。仅当
          配置里 ``ai.api_key`` 仍为空/占位时填入——用户自己保存的 Key 永远优先。
        - ``AITR_HOSTED_VISION_*``：厂商托管识图网关（同上，**永不入库**）。
        - ``AITR_HOSTED_VOICE_* / AITR_HOSTED_ASR_*``：托管克隆语音 / 语音识别
          （混合形态，只动种子 ``_lan_seed`` 标记过的段，**永不入库**）。
        - ``AITR_HOSTED_EMBED_*``：托管嵌入（LAN 嵌入端点不可达时改走网关，
          **永不入库**）——不回放的话热重载会把 ai.embedding_* 抹回 LAN，
          语义记忆召回重新降级为关键词。
        """
        desktop = self._env_truthy("AITR_DESKTOP_MODE")
        host = os.environ.get("AITR_WEB_HOST")
        port = os.environ.get("AITR_WEB_PORT")
        token = os.environ.get("AITR_WEB_TOKEN")
        hosted_key = (os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
        hosted_vision = (os.environ.get("AITR_HOSTED_VISION_BASE_URL") or "").strip()
        hosted_voice = (os.environ.get("AITR_HOSTED_VOICE_BASE_URL") or "").strip()
        hosted_asr = (os.environ.get("AITR_HOSTED_ASR_BASE_URL") or "").strip()
        hosted_embed = (os.environ.get("AITR_HOSTED_EMBED_BASE_URL") or "").strip()
        if desktop or host or port or token:
            web = self.config.get("web_admin")
            if not isinstance(web, dict):
                web = {}
                self.config["web_admin"] = web
            if host:
                web["host"] = host
            if port:
                try:
                    web["port"] = int(str(port).strip())
                except (TypeError, ValueError):
                    self.logger.warning("AITR_WEB_PORT 非法（忽略）: %r", port)
            if token:
                web["auth_token"] = token
            if desktop:
                web["enabled"] = True
        if hosted_key:
            self._apply_hosted_ai_env(hosted_key)
        if hosted_vision:
            self._apply_hosted_vision_env(hosted_vision)
        if hosted_voice or hosted_asr:
            self._apply_hosted_media_env(hosted_voice, hosted_asr)
        if hosted_embed:
            self._apply_hosted_embed_env(hosted_embed)

    def _apply_hosted_ai_env(self, hosted_key: str) -> None:
        """把 ``AITR_HOSTED_AI_*`` 注入内存中的 ``ai.*``（用户自有 Key 不覆盖）。"""
        from src.utils.golive import _is_placeholder

        ai = self.config.get("ai")
        if not isinstance(ai, dict):
            ai = {}
            self.config["ai"] = ai
        if not _is_placeholder(ai.get("api_key")):
            return
        ai["api_key"] = hosted_key
        base = (os.environ.get("AITR_HOSTED_AI_BASE_URL") or "").strip()
        model = (os.environ.get("AITR_HOSTED_AI_MODEL") or "").strip()
        if base:
            ai["base_url"] = base
        if model:
            ai["model"] = model
        # 内存标记：供工作台试用条 / 观测；不进 save_ai_credentials 写入路径
        ai["_hosted_trial"] = True
        self.logger.info(
            "已启用托管 AI 试用（AITR_HOSTED_AI_KEY；用户未配置自有 Key）")

    def _apply_hosted_vision_env(self, base_url: str) -> None:
        """把 ``AITR_HOSTED_VISION_*`` 回放进内存 ``vision.*``（用户自配后端不覆盖）。

        与 :meth:`_apply_hosted_ai_env` 同款理由（2026-07-31 实锤）：托管识图的网关
        地址与设备令牌是启动时的**内存注入**，刻意不落盘（令牌不入库）。没有这次回放，
        任何一次配置热重载都会把它抹掉——而写 ``config.local.yaml`` 就会触发热重载，
        于是「一键开齐入站识别」按钮把自己刚要用的后端弄没了，只剩 overlay 里的
        ``vision.enabled=true`` 配一盏「开了但后端未就绪」的黄灯，重启前无法自愈。

        令牌复用 ``AITR_HOSTED_AI_KEY``（识图与聊天同一枚，不另存一份免得换新后漂移）。
        ``enabled`` 默认用 setdefault（运营意图归 overlay）；``AITR_HOSTED_VISION_AUTO=1``
        时强制重开——那是种子档 ``_hosted_auto`` 的预授权，不是运营「全部关闭」。
        """
        vision = self.config.get("vision")
        if not isinstance(vision, dict):
            vision = {}
            self.config["vision"] = vision
        # 用户/运维自配的识图后端（无托管标记）→ 尊重，绝不覆盖。
        # 例外：内测种子的 LAN 后端（_lan_seed，混合形态）——env 在场＝启动时已判定
        # 「LAN 不可达 → 网关接管」，热重载按同一结论回放（重载路径不做网络探测）。
        if not (vision.get("_hosted_vision") or vision.get("_lan_seed")) and (
            str(vision.get("base_url") or "").strip()
            or vision.get("base_urls")
            or str(vision.get("api_key") or "").strip()
        ):
            return
        token = (os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
        if not token:
            return
        # 混合形态：重载刚从盘上读回 LAN 种子值 → 先暂存，供回内网时可逆还原
        if vision.get("_lan_seed") and not vision.get("_hosted_vision"):
            vision["_lan_backend"] = {
                "provider": vision.get("provider"),
                "base_url": vision.get("base_url"),
                "base_urls": list(vision.get("base_urls") or []) or None,
                "api_key": vision.get("api_key"),
                "model": vision.get("model"),
            }
        model = (os.environ.get("AITR_HOSTED_VISION_MODEL") or "").strip()
        vision["provider"] = "openai_compatible"
        vision["base_url"] = base_url
        # base_urls 一并指网关：种子 LAN 列表优先级高于单数 base_url，不改写会
        # 让 VisionClient 继续打死掉的 192.168.0.x
        vision["base_urls"] = [base_url]
        vision["api_key"] = token
        if model:
            vision["model"] = model
        vision["_hosted_vision"] = True
        # 托管自动接入回放（2026-08-22）：ensure_hosted_vision 经 _hosted_auto
        # 开启的部署，热重载后 enabled 会被 overlay 的 false 抹回——回放必须
        # 同样重放「自动开启」，否则任何一次写 overlay 都等于把托管识图关掉。
        if (os.environ.get("AITR_HOSTED_VISION_AUTO") or "").strip() == "1":
            vision["enabled"] = True
        else:
            vision.setdefault("enabled", True)

    def _apply_hosted_media_env(self, voice_base: str, asr_base: str) -> None:
        """回放托管克隆语音 / 语音识别注入（与 ``_apply_hosted_vision_env`` 同款理由）。

        端点次序按注入时的 LAN 探测结论回放（``AITR_HOSTED_*_FIRST``）——热重载在
        web 请求检查点触发，**绝不能**在这条路径上做秒级网络探测。变更逻辑的单一
        事实源在 ``hosted_gateway.apply_hosted_voice/apply_hosted_asr``（惰性导入防
        环）。生产实例没有这些 env → 全程 no-op。
        """
        try:
            from src.ai.hosted_gateway import apply_hosted_asr, apply_hosted_voice
        except Exception:
            return
        if voice_base:
            apply_hosted_voice(
                self.config, voice_base,
                gateway_first=(os.environ.get("AITR_HOSTED_VOICE_FIRST")
                               or "").strip() == "1",
                hub_fish_off=(os.environ.get("AITR_HOSTED_HUBFISH_OFF")
                              or "").strip() == "1",
                # 托管自动接入回放（2026-08-19）：ensure_hosted_voice 经
                # _hosted_auto 开启的部署，热重载后 enabled 会被 overlay 的
                # false 抹回——回放必须同样重放「自动开启」，否则任何一次写
                # overlay 都等于把托管语音关掉（与识图 env 回放同教训）。
                auto_enable=(os.environ.get("AITR_HOSTED_VOICE_AUTO")
                             or "").strip() == "1")
        if asr_base:
            apply_hosted_asr(
                self.config, asr_base,
                gateway_first=(os.environ.get("AITR_HOSTED_ASR_FIRST")
                               or "").strip() == "1",
                # 同 voice：_hosted_auto 开启的转写在热重载后 enabled 会被
                # overlay 的 false 抹回，回放必须重放「自动开启」。
                auto_enable=(os.environ.get("AITR_HOSTED_ASR_AUTO")
                             or "").strip() == "1")

    def _apply_hosted_embed_env(self, gw_base: str) -> None:
        """回放托管嵌入注入（与 ``_apply_hosted_media_env`` 同款理由）。

        不做网络探测：注入时已判过「LAN 端点全不可达」，回放只是把那个结论重放
        进新读入的配置。变更逻辑单一事实源在 ``hosted_gateway.apply_hosted_embed``。
        """
        try:
            from src.ai.hosted_gateway import apply_hosted_embed
        except Exception:
            # 回放失败＝热重载后 ai.embedding_* 被抹回 LAN，语义召回静默降级成关键词。
            # 不抛（配置热重载绝不能被它带崩），但必须留痕，否则没人查得到降级从哪来。
            self.logger.debug("托管嵌入回放不可用（hosted_gateway 导入失败）",
                              exc_info=True)
            return
        apply_hosted_embed(self.config, gw_base)

    def _ensure_baseline(self) -> None:
        """桌面态（AITR_DESKTOP_MODE）产品基线增量补齐——修「种子只影响新装」缺口。

        ``_ensure_seeded`` 只在 config.yaml 缺失时播种，存量安装的 config 是旧种子
        快照，永远不会自己长出新加入基线的功能开关（实锤：工作目标进种子后，已装
        用户仍看不见）。本方法按 ``feature_registry.baseline_patch`` 补齐：

        - 判据＝合并视图（config+overlay）里**键缺失**。缺失=用户从未表达过意见，
          补齐零冲突；显式 true/false 都尊重不动——不需要三方合并那套复杂度。
        - 写入面＝overlay（``save_overlay_patch``：原子落盘 + 即时深合并进内存 +
          刷新热重载基线），主 config.yaml 注释/结构永不被改写。
        - 幂等：补过即存在，下次启动 patch 为空、零写盘。
        - 仅桌面态：服务器实例（zhiliao 等）无此 env，行为零变化；
          example/自建档的保守默认不受影响。
        - 永不抛：基线补不上=功能保持关闭，绝不拖垮启动。
        """
        try:
            if not self._env_truthy("AITR_DESKTOP_MODE"):
                return
            from src.utils.feature_registry import as_nested, baseline_patch
            patch = baseline_patch(self.config)
            if not patch:
                return
            if self.save_overlay_patch(as_nested(patch)):
                self.logger.info(
                    "产品基线已补齐进 overlay: %s", ", ".join(sorted(patch)))
            else:
                self.logger.warning("产品基线补齐写入失败（忽略，功能保持关闭）")
        except Exception as exc:
            self.logger.warning("产品基线补齐异常（忽略）: %s", exc)

    def _ensure_deploy_profile(self) -> None:
        """部署能力预设档首启播种（WP-1 纯云起步档，2026-08）。

        ``AITR_DEPLOY_PROFILE=<name>``（桌面壳 launcher 打包态注入 cloud_light，
        独立 VM 可手动设）时，把 ``config/profiles/<name>.yaml`` 深合并进
        config.local.yaml overlay（``save_overlay_patch``＝ruamel 保注释写入）。

        三道闸，缺一不应用：
        - env 显式声明（生产双实例/开发态无此 env → 本方法恒 no-op）；
        - 本次 config 为**全新播种**（``_seeded_fresh``）——升级安装/既有配置的
          机器永不被覆写，用户对任何键表达过的选择都保留；
        - 合并视图尚无同名 ``deploy.profile`` 标记（幂等：同进程内热重载会再进
          load()，标记挡住二次落盘）。
        - 永不抛：预设缺失/坏档=按无预设跑（load_profile 已软失败返回空）。
        """
        try:
            name = (os.environ.get("AITR_DEPLOY_PROFILE") or "").strip().lower()
            if not name or not self._seeded_fresh:
                return
            from src.utils.deploy_profile import active_profile, load_profile
            if active_profile(self.config) == name:
                return
            patch = load_profile(name)
            if not patch:
                self.logger.warning(
                    "部署预设档 %r 缺失或解析失败（忽略，按无预设跑）", name)
                return
            if self.save_overlay_patch(patch):
                self.logger.info(
                    "部署预设档已播种进 overlay: %s（顶层键: %s）",
                    name, ", ".join(sorted(map(str, patch))))
            else:
                self.logger.warning("部署预设档写入失败（忽略）: %s", name)
        except Exception as exc:
            self.logger.warning("部署预设档播种异常（忽略）: %s", exc)

    def _overlay_path(self) -> Path:
        """凭证 overlay 路径：主配置同目录下的 config.local.yaml。"""
        return self.config_path.parent / "config.local.yaml"

    @staticmethod
    def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
        """递归合并 over 到 base（就地改 base）；dict 递归，其余以 over 覆盖。"""
        for k, v in (over or {}).items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                ConfigManager._deep_merge(base[k], v)
            else:
                base[k] = v
        return base

    def _merge_overlay(self) -> None:
        """若存在 config.local.yaml，深合并覆盖到 self.config（缺失/损坏则静默跳过）。"""
        path = self._overlay_path()
        try:
            if not path.exists():
                return
            with open(path, "r", encoding="utf-8") as f:
                overlay = yaml.safe_load(f) or {}
            if isinstance(overlay, dict) and overlay:
                self._deep_merge(self.config, overlay)
                self.logger.info("已合并凭证 overlay: %s", path)
        except Exception as exc:
            self.logger.warning("凭证 overlay 合并失败（忽略）: %s", exc)

    def save_channel_credentials(
        self, channel: str, values: Dict[str, Any],
    ) -> tuple:
        """P1-1 接入向导：把某渠道的凭证字段写入 config.local.yaml 并即时生效。

        只接受 channel_setup 声明的已知字段（防注入任意键），写 overlay（保住主
        config 注释），随后深合并进 self.config 让本进程立即可见。
        返回 (成功?, 说明, issues:list)。
        """
        try:
            from src.utils.channel_setup import apply_channel_values
        except Exception as exc:
            return False, f"channel_setup 不可用: {exc}", []
        path = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            # base_config=运行合并视图：主 config 已有的凭据也算「就绪」，
            # 使 enable_on_ready 桥接（如 Telegram protocol_enabled）判定不漏。
            ok, msg = apply_channel_values(
                overlay, channel, values, base_config=self.config)
            if not ok:
                return False, msg, []
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 接入向导写入的凭证 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(path)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入凭证 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}", []
        issues = []
        try:
            from src.utils.config_check import check_config
            issues = check_config(self.config, config_path=self.config_path)
        except Exception:
            self.logger.debug("写入后自检失败（忽略）", exc_info=True)
        return True, "已保存", issues

    def save_ai_credentials(self, values: Dict[str, Any]) -> tuple:
        """P0-1 首启向导：把 AI 大模型凭证写入 config.local.yaml 的 ``ai:`` 段并即时生效。

        走 overlay 而非改写主 config.yaml（保住注释/结构，密钥不进 git 跟踪文件）。
        只接受白名单字段（防注入任意键）；空值字段跳过（部分更新，不清空已有值）。
        返回 (成功?, 说明)。
        """
        allowed = ("provider", "api_key", "base_url", "model")
        clean: Dict[str, Any] = {}
        for k in allowed:
            v = (values or {}).get(k)
            if v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            clean[k] = v
        if not clean:
            return False, "没有可保存的字段"
        prov = clean.get("provider")
        if prov and prov not in ("gemini", "openai_compatible"):
            return False, f"未知 provider: {prov}"
        path = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            if not isinstance(overlay, dict):
                overlay = {}
            ai = dict(overlay.get("ai") or {})
            ai.update(clean)
            overlay["ai"] = ai
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 接入向导写入的凭证 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(path)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入 AI 凭证 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}"
        self.logger.info("AI 凭证已写入 overlay（字段: %s）", ", ".join(sorted(clean)))
        return True, "已保存"

    def save_branding(self, values: Dict[str, Any]) -> tuple:
        """C1-1 白标：把品牌字段写入 config.local.yaml 的 ``brand:`` 段并即时生效。

        只接受白名单字段（防注入任意键），写 overlay（保住主 config 注释），随后深合并
        进 self.config 让本进程立即可见。返回 (成功?, 说明)。
        """
        allowed = {
            "site_name", "site_name_short", "company_name", "product_name",
            "sidebar_name", "website_url", "primary_color", "logo_url",
            "login_subtitle", "hide_powered_by",
        }
        clean: Dict[str, Any] = {}
        for k, v in (values or {}).items():
            if k not in allowed:
                continue
            if k == "hide_powered_by":
                clean[k] = bool(v)
            else:
                clean[k] = ("" if v is None else str(v)).strip()
        path = self._overlay_path()
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            brand = dict(overlay.get("brand") or {})
            brand.update(clean)
            overlay["brand"] = brand
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 白标/凭证 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(path)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入品牌 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}"
        return True, "已保存"

    def set_overlay_flag(self, path: str, value: Any) -> tuple:
        """把单个开关写入 config.local.yaml overlay 并即时生效（陪伴能力分阶段开启用）。

        走 overlay 而非改写 config.yaml：保住主配置注释/结构、与凭证/白标同机制，重启后
        load() 再次深合并。``path`` 为点分隔嵌套键（如 ``companion.proactive_topic.enabled``）。
        返回 (成功?, 说明)。调用方（看板路由）已用能力注册表白名单约束 path，避免任意键注入。

        P2 2026-08-01 保注释化：优先走 ruamel round-trip（``set_yaml_key_preserving``）——
        旧实现 ``yaml.dump`` 整文件重写会把 overlay 里的人工运维注释全部剃光
        （2026-08-01 上午实锤：一次 enable_dry 冲掉 config.local.yaml 约 30 行注释）。
        ruamel 缺失/任何异常 → 回落旧 yaml.dump 路径（行为与历史完全一致，零回归）。
        """
        keys = [k for k in str(path or "").split(".") if k]
        if not keys:
            return False, "空配置路径"
        p = self._overlay_path()
        if set_yaml_key_preserving(p, keys, value):
            try:
                self._deep_merge(self.config, _nested_patch(keys, value))
            except Exception:
                self.logger.debug("overlay 内存合并异常（下次热重载兜底）", exc_info=True)
            self.logger.info("运营开关已更新(保注释): %s = %r", ".".join(keys), value)
            return True, "已保存"
        try:
            overlay: Dict[str, Any] = {}
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            if not isinstance(overlay, dict):
                overlay = {}
            node = overlay
            for k in keys[:-1]:
                nxt = node.get(k)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[k] = nxt
                node = nxt
            node[keys[-1]] = value
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 运营开关 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            tmp.replace(p)
            self._deep_merge(self.config, overlay)
        except Exception as exc:
            self.logger.error("写入开关 overlay 失败: %s", exc)
            return False, f"写入失败: {exc}"
        self.logger.info("运营开关已更新: %s = %r", ".".join(keys), value)
        return True, "已保存"

    def save_overlay_patch(self, patch: Dict[str, Any], *,
                           replace_paths: Any = ()) -> bool:
        """把「本次真正改动」的最小 patch 深合并进 config.local.yaml 并即时生效。

        渠道路由等运行时设置保存的统一出口：写 overlay 而非整文件回写主
        config.yaml（保住主配置注释/结构，运行时值不固化进 git 跟踪文件）。
        合并语义与 ``_deep_merge`` 一致：dict 递归、其余类型（**含 list，整体
        替换不 extend**）以 patch 覆盖。patch 为空 → 直接 True 不落盘。

        ``replace_paths``：点分路径集合（如 ``("intent.keywords",)``），命中的
        子树按**整体赋值**而非 dict 递归合并——供「整字典替换」语义的配置
        （意图关键词表等：删掉的键必须真被删掉，深合并会让陈旧键赖着不走）。
        overlay 落盘与内存刷新走同一 replace 感知合并，删除即时生效不等重启。

        写盘为 tmp 文件 + ``os.replace`` 原子替换；成功后深合并进 self.config
        保持内存一致，并同步刷新 ``_overlay_loaded_mtime`` 基线——自己刚写的
        overlay 不触发 ``check_and_hot_reload`` 的整装重载（重载走 load() 同
        路径、值与内存一致，幂等无害但多余）。返回 True/False（异常记日志并
        返回 False，绝不抛）。
        """
        if not patch:
            return True
        if not isinstance(patch, dict):
            self.logger.error(
                "save_overlay_patch: patch 须为 dict，收到 %s", type(patch).__name__)
            return False
        replace = {str(p) for p in (replace_paths or ())}
        path = self._overlay_path()
        # P3 2026-08-01 保注释化：优先 ruamel round-trip（与 set_overlay_flag 同待遇；
        # 渠道路由等运行时设置保存同样不该剃光 overlay 注释）。失败回落旧 yaml.dump。
        if merge_yaml_patch_preserving(path, patch, replace,
                                       self._merge_patch_replace_aware):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    merged = yaml.safe_load(f) or {}
                if isinstance(merged, dict):
                    self._merge_patch_replace_aware(self.config, merged, replace)
                self._overlay_loaded_mtime = self._overlay_mtime()
            except Exception:
                self.logger.debug("overlay 内存合并异常（下次热重载兜底）", exc_info=True)
            self.logger.info(
                "运行时设置已写入 overlay(保注释)（顶层键: %s）",
                ", ".join(sorted(map(str, patch))))
            return True
        try:
            overlay: Dict[str, Any] = {}
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    overlay = yaml.safe_load(f) or {}
            if not isinstance(overlay, dict):
                overlay = {}
            self._merge_patch_replace_aware(overlay, patch, replace)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# 运行时设置写入（渠道中心等）的 overlay（深合并覆盖 config.yaml）。\n")
                f.write("# 请勿提交到 git（应在 .gitignore）。\n")
                yaml.dump(overlay, f, default_flow_style=False,
                          allow_unicode=True, sort_keys=False)
            os.replace(tmp, path)
            # 内存刷新仍合并整个 overlay（吸收落盘前的人工手改，行为与旧版一致），
            # 但 replace 路径按赋值处理——overlay 里该子树刚被整树替换，深合并会把
            # 内存里已删除的键留到重启。
            self._merge_patch_replace_aware(self.config, overlay, replace)
            self._overlay_loaded_mtime = self._overlay_mtime()
        except Exception as exc:
            self.logger.error("写入运行时设置 overlay 失败: %s", exc)
            return False
        self.logger.info(
            "运行时设置已写入 overlay（顶层键: %s）", ", ".join(sorted(map(str, patch))))
        return True

    def _merge_patch_replace_aware(self, dst: Dict[str, Any],
                                   patch: Dict[str, Any],
                                   replace: set, _prefix: str = "") -> None:
        """_deep_merge 的 replace 感知版：命中 replace 点分路径的键整体赋值。"""
        for k, v in patch.items():
            path = f"{_prefix}{k}"
            if (path not in replace and isinstance(v, dict)
                    and isinstance(dst.get(k), dict)):
                self._merge_patch_replace_aware(dst[k], v, replace, path + ".")
            else:
                dst[k] = v

    def _run_startup_self_check(self) -> None:
        """启动时跑配置自检并把 error/warn 摘要写日志（永不抛、永不阻断启动）。"""
        try:
            from src.utils.config_check import check_config
        except Exception:
            return
        try:
            issues = check_config(self.config, config_path=self.config_path)
        except Exception as exc:
            self.logger.debug("配置自检执行异常（忽略）: %s", exc)
            return
        errors = [i for i in issues if i.severity == "error"]
        warns = [i for i in issues if i.severity == "warn"]
        for i in errors:
            self.logger.error("配置自检[错误] %s: %s", i.path, i.message)
        for i in warns:
            self.logger.warning("配置自检[警告] %s: %s", i.path, i.message)
        if errors or warns:
            self.logger.warning(
                "配置自检发现 %d 错误 / %d 警告；详情运行 `python main.py --check`",
                len(errors), len(warns))

    def _validate_config(self) -> bool:
        """验证配置文件"""
        required_sections = ['telegram', 'ai', 'skills']

        # 检查必需的部分
        for section in required_sections:
            if section not in self.config:
                self.logger.error(f"配置缺少必需部分: {section}")
                return False

        # 验证Telegram配置
        # 桌面/托管版：全局 telegram 凭据故意留空——多账号经 credpool 逐账号提供
        # （account_registry meta.credpool_cred / session_string），全局 api_id 是占位。
        # 此时占位/缺失是**预期态**，旧实现每次启动刷 ERROR（`请配置有效的Telegram api_id`）
        # 淹没日志、惊到运营、也污染日志监控。managed 下降级 INFO 并放行 telegram 段；
        # 单账号/服务器部署仍走严格校验。（2026-08-07 198/104 日志监控实锤：这是最吵的伪 ERROR）
        managed = (self._env_truthy("AITR_DESKTOP_MODE")
                   or self._env_truthy("AITR_MANAGED_EDITION"))
        telegram_config = self.config.get('telegram', {})
        required_telegram_keys = ['api_id', 'api_hash', 'phone_number']

        for key in required_telegram_keys:
            missing = key not in telegram_config
            value = None if missing else telegram_config[key]
            placeholder = missing or value == f"YOUR_{key.upper()}" or not value
            if not placeholder:
                continue
            if managed:
                self.logger.info(
                    "桌面/托管版：全局 telegram.%s 未配置（账号走 credpool 逐账号提供），"
                    "跳过全局校验", key)
                continue
            if missing:
                self.logger.error(f"Telegram配置缺少必需键: {key}")
            else:
                self.logger.error(f"请配置有效的Telegram {key}")
            return False

        # 验证AI配置
        ai_config = self.config.get('ai', {})
        if 'api_key' not in ai_config or ai_config['api_key'] == "YOUR_AI_API_KEY":
            self.logger.error("请配置有效的 AI API 密钥")
            return False

        return True

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值，支持点分隔的嵌套键"""
        keys = key.split('.')
        value = self.config

        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default

    def set(self, key: str, value: Any) -> None:
        """设置配置值，支持点分隔的嵌套键"""
        keys = key.split('.')
        config = self.config

        # 遍历到倒数第二个键
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]

        # 设置最后一个键的值
        config[keys[-1]] = value

    def save(self) -> bool:
        """保存配置到文件"""
        try:
            # 确保配置目录存在
            self.config_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            self.logger.info(f"配置已保存到: {self.config_path}")
            return True

        except Exception as e:
            self.logger.error(f"保存配置失败: {e}")
            return False

    def get_telegram_config(self) -> Dict[str, Any]:
        """获取Telegram配置"""
        return self.config.get('telegram', {})

    def get_ai_config(self) -> Dict[str, Any]:
        """获取AI配置"""
        return self.config.get('ai', {})

    def get_line_rpa_config(self) -> Dict[str, Any]:
        """个人 LINE 客户端 RPA（ADB）；可选，默认空。"""
        return self.config.get("line_rpa") or {}

    def get_messenger_rpa_config(self) -> Dict[str, Any]:
        """Facebook Messenger RPA（ADB + Vision）；可选，默认空。"""
        return self.config.get("messenger_rpa") or {}

    def get_facebook_messenger_config(self) -> Dict[str, Any]:
        """Facebook Page Messenger Webhook（官方 Graph API）；可选，默认空。"""
        return self.config.get("facebook_messenger") or {}

    def get_skills_config(self) -> Dict[str, Any]:
        """获取Skills配置"""
        return self.config.get('skills', {})

    def get_intent_config(self) -> Dict[str, Any]:
        """获取意图识别配置"""
        return self.config.get('intent', {})

    def get_templates_config(self) -> Dict[str, Any]:
        """获取模板配置"""
        return self.config.get('templates', {})

    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.config.get('logging', {})

    def get_dynamic_templates_config(self) -> Dict[str, Any]:
        """加载动态话术模板配置（config/templates.yaml），支持热更新（按文件 mtime 重载）"""
        templates_file = self.config_path.parent / "templates.yaml"
        if not templates_file.exists():
            # 回退：按 config_manager 所在包路径找 config/templates.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                templates_file = base / "config" / "templates.yaml"
            except Exception:
                pass
        if not templates_file.exists():
            self.logger.debug("动态话术模板文件不存在，将使用主配置中的模板: %s", _path_str(templates_file))
            return {}
        try:
            mtime = os.path.getmtime(templates_file)
            if self._templates_cache is not None and mtime <= self._templates_mtime:
                return self._templates_cache
            with open(templates_file, 'r', encoding='utf-8') as f:
                self._templates_cache = yaml.safe_load(f) or {}
            self._templates_mtime = mtime
            template_keys = list(self._templates_cache.keys())
            self.logger.info("动态话术模板已加载: 模板类别数=%s, 文件=%s", len(template_keys), templates_file.name)
            return self._templates_cache
        except Exception as e:
            self.logger.warning("加载动态话术模板失败: %s", e)
            return self._templates_cache if self._templates_cache is not None else {}

    def get_exchange_rates_config(self) -> Dict[str, Any]:
        """加载动态汇率配置（config/exchange_rates.yaml），支持热更新（按文件 mtime 重载）"""
        exchange_rates_file = self.config_path.parent / "exchange_rates.yaml"
        if not exchange_rates_file.exists():
            # 回退：按 config_manager 所在包路径找 config/exchange_rates.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                exchange_rates_file = base / "config" / "exchange_rates.yaml"
            except Exception:
                pass
        if not exchange_rates_file.exists():
            self.logger.debug("动态汇率配置文件不存在: %s", _path_str(exchange_rates_file))
            return {}
        try:
            mtime = os.path.getmtime(exchange_rates_file)
            if self._exchange_rates_cache is not None and mtime <= self._exchange_rates_mtime:
                return self._exchange_rates_cache
            with open(exchange_rates_file, 'r', encoding='utf-8') as f:
                self._exchange_rates_cache = yaml.safe_load(f) or {}
            self._exchange_rates_mtime = mtime
            channels = self._exchange_rates_cache.get("channels") or {}
            self.logger.info("动态汇率配置已加载: 通道数=%s, 文件=%s", len(channels), exchange_rates_file.name)
            return self._exchange_rates_cache
        except Exception as e:
            self.logger.warning("加载动态汇率配置失败: %s", e)
            return self._exchange_rates_cache if self._exchange_rates_cache is not None else {}

    async def reload(self) -> bool:
        """重新加载配置文件"""
        self.config = {}
        self._quota_rules_cache = None
        self._quota_rules_mtime = 0
        self._templates_cache = None
        self._templates_mtime = 0
        self._exchange_rates_cache = None
        self._exchange_rates_mtime = 0
        self._strategies_cache = None
        self._strategies_mtime = 0
        return await self.load()

    # _hosted_cred / _hosted_proxy 是托管注入三件套的标记与出口（hosted_gateway.
    # ensure_hosted_telegram），与 api_id/api_hash 同批写入，必须同批携带：只带值不带
    # 标记的话，热重载后渠道设置页会把本该隐藏的池凭据字段重新亮出来（channel_setup.
    # _AUTO_PROVISIONED 按它判定）；只带凭据不带出口的话，热重载后大陆机器的登录
    # 会从代理出口悄悄变回直连（被墙=登录挂、没被墙=出口 IP 漂移，都是错）。
    _HOT_RELOAD_PROTECTED_KEYS = {"api_id", "api_hash", "phone_number", "session_name",
                                  "_hosted_cred", "_hosted_proxy"}

    @staticmethod
    def _validate_hot_reload_config(data) -> str:
        """热重载校验：检查配置结构完整性，返回拒绝原因（空字符串=通过）"""
        if not isinstance(data, dict):
            return "配置不是有效字典"
        if "telegram" not in data:
            return "缺少 telegram 节点"
        tg = data["telegram"]
        if not isinstance(tg, dict):
            return "telegram 节点不是字典"
        ai = data.get("ai", {})
        if ai and not isinstance(ai, dict):
            return "ai 节点不是字典"
        for key in ("max_tokens",):
            val = ai.get(key)
            if val is not None:
                try:
                    v = int(val)
                    if v <= 0:
                        return f"ai.{key} 必须为正整数，当前值: {val}"
                except (ValueError, TypeError):
                    return f"ai.{key} 不是有效数字: {val}"
        reply_cfg = data.get("reply", {})
        if reply_cfg and not isinstance(reply_cfg, dict):
            return "reply 节点不是字典"
        strategies = reply_cfg.get("strategies", {})
        if strategies and not isinstance(strategies, dict):
            return "reply.strategies 不是字典"
        return ""

    def on_reload(self, callback) -> None:
        """注册热重载回调。callback() 在配置成功重载后同步调用。"""
        self._on_reload_callbacks.append(callback)

    def _overlay_mtime(self) -> float:
        """config.local.yaml 的 mtime（不存在 → 0）。"""
        try:
            p = self._overlay_path()
            return os.path.getmtime(p) if p.exists() else 0.0
        except Exception:
            return 0.0

    def check_and_hot_reload(self) -> bool:
        """检查 config.yaml **或 config.local.yaml** 是否修改，若有则热重载（保护不可变字段）。
        返回 True 表示发生了重载。适合在消息处理主循环 / web 请求检查点定期调用。

        2026-07 修复两个缺陷：
        - **overlay 丢失**：旧实现只 safe_load 主文件直接赋值 self.config——config.local.yaml
          的所有运营开关（翻译引擎双活/发送闸门/backfill…）在热重载瞬间全部蒸发，直到下次
          重启才恢复。现重载走与 load() 相同路径（主文件 + _merge_overlay + _apply_env_overrides）。
        - **overlay 盲区**：只监视主文件 mtime——改 overlay（运营开关的正道）不触发重载。
          现任一文件变化都触发。
        """
        import time as _t
        now = _t.time()
        if now - self._last_hot_reload_check < self._hot_reload_interval:
            return False
        self._last_hot_reload_check = now
        try:
            if not self.config_path.exists():
                return False
            mtime = os.path.getmtime(self.config_path)
            ov_mtime = self._overlay_mtime()
            if mtime <= self._config_mtime and ov_mtime <= getattr(self, "_overlay_loaded_mtime", 0.0):
                return False
            with open(self.config_path, 'r', encoding='utf-8') as f:
                new_data = yaml.safe_load(f)
            reject_reason = self._validate_hot_reload_config(new_data)
            if reject_reason:
                self.logger.warning("热重载拒绝: %s", reject_reason)
                return False
            old_tg = self.config.get('telegram', {})
            new_tg = new_data.get('telegram', {})
            for key in self._HOT_RELOAD_PROTECTED_KEYS:
                if key in old_tg:
                    new_tg[key] = old_tg[key]
            new_data['telegram'] = new_tg
            self.config = new_data
            # 与 load() 同路径：overlay 深合并 + env 覆盖（修「热重载丢 overlay」）
            self._merge_overlay()
            self._apply_env_overrides()
            self._config_mtime = mtime
            self._overlay_loaded_mtime = ov_mtime
            self._quota_rules_cache = None
            self._quota_rules_mtime = 0
            self._templates_cache = None
            self._templates_mtime = 0
            self._exchange_rates_cache = None
            self._exchange_rates_mtime = 0
            self.logger.info("配置热重载完成 (config.yaml mtime=%s overlay mtime=%s)",
                             mtime, ov_mtime)
            for cb in self._on_reload_callbacks:
                try:
                    cb()
                except Exception as cb_err:
                    self.logger.debug("热重载回调异常: %s", cb_err)
            return True
        except Exception as e:
            self.logger.warning("配置热重载失败: %s", e)
            return False

    def get_quota_rules(self) -> Dict[str, Any]:
        """加载额度规则配置（config/quota_rules.yaml），支持热更新（按文件 mtime 重载）"""
        quota_file = self.config_path.parent / "quota_rules.yaml"
        if not quota_file.exists():
            # 回退：按 config_manager 所在包路径找 config/quota_rules.yaml
            try:
                base = Path(__file__).resolve().parent.parent.parent
                quota_file = base / "config" / "quota_rules.yaml"
            except Exception:
                pass
        if not quota_file.exists():
            self.logger.debug("额度规则文件不存在，将使用 AI 回复额度类问题: %s", _path_str(quota_file))
            return {}
        try:
            mtime = os.path.getmtime(quota_file)
            if self._quota_rules_cache is not None and mtime <= self._quota_rules_mtime:
                return self._quota_rules_cache
            with open(quota_file, 'r', encoding='utf-8') as f:
                self._quota_rules_cache = yaml.safe_load(f) or {}
            self._quota_rules_mtime = mtime
            channels = self._quota_rules_cache.get("channels") or {}
            self.logger.info("额度规则已加载: 通道数=%s, 文件=%s", len(channels), quota_file.name)
            return self._quota_rules_cache
        except Exception as e:
            self.logger.warning("加载额度规则失败: %s", e)
            return self._quota_rules_cache if self._quota_rules_cache is not None else {}

    def get_quota_rules_file_path(self) -> Optional[Path]:
        """返回 quota_rules.yaml 的路径，供对话命令写回配置使用；不存在则返回 None"""
        quota_file = self.config_path.parent / "quota_rules.yaml"
        if not quota_file.exists():
            try:
                base = Path(__file__).resolve().parent.parent.parent
                quota_file = base / "config" / "quota_rules.yaml"
            except Exception:
                pass
        return quota_file if quota_file.exists() else None

    def invalidate_quota_rules_cache(self) -> None:
        """使额度规则缓存失效，下次 get_quota_rules 将重新从文件加载"""
        self._quota_rules_cache = None
        self._quota_rules_mtime = 0

    def update_quota_rules_special_groups(
        self, add: Optional[list] = None, remove: Optional[list] = None
    ) -> tuple:
        """
        增删特殊客户群名单并写回 quota_rules.yaml。
        add/remove 为群名字符串列表；返回 (成功?, 说明文案)。
        """
        path = self.get_quota_rules_file_path()
        if not path:
            return False, "未找到 quota_rules.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            groups = list(data.get("special_groups") or [])
            if add:
                for name in add:
                    name = (name or "").strip()
                    if name and name not in groups:
                        groups.append(name)
            if remove:
                for name in remove:
                    groups = [g for g in groups if (g or "").strip() != (name or "").strip()]
            data["special_groups"] = groups
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self.invalidate_quota_rules_cache()
            return True, f"已更新特殊群名单，当前共 {len(groups)} 个"
        except Exception as e:
            self.logger.warning("写回额度规则失败: %s", e)
            return False, f"写回配置失败: {e}"

    def update_quota_rules_blacklist(
        self,
        add_group: Optional[str] = None,
        ep_text: Optional[str] = None,
        jc_text: Optional[str] = None,
        remove_group: Optional[str] = None,
    ) -> tuple:
        """
        添加或删除黑名单群并写回 quota_rules.yaml。
        add_group 时 ep_text/jc_text 为可选话术；remove_group 为要删除的群名。返回 (成功?, 说明文案)。
        """
        path = self.get_quota_rules_file_path()
        if not path:
            return False, "未找到 quota_rules.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            blacklist = dict(data.get("blacklist_groups") or {})
            if remove_group:
                key = (remove_group or "").strip()
                if key in blacklist:
                    del blacklist[key]
                data["blacklist_groups"] = blacklist
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                self.invalidate_quota_rules_cache()
                return True, f"已删除黑名单群：{remove_group}"
            if add_group:
                key = (add_group or "").strip()
                if not key:
                    return False, "群名为空"
                ep_text = (ep_text or "当前EP渠道受限，请使用其他渠道或联系客服处理。").strip()
                jc_text = (jc_text or "当前JC额度请以实际提交为准。").strip()
                blacklist[key] = {"ep": ep_text, "jc": jc_text}
                data["blacklist_groups"] = blacklist
                with open(path, "w", encoding="utf-8") as f:
                    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
                self.invalidate_quota_rules_cache()
                return True, f"已添加黑名单群：{key}"
            return False, "请指定 添加黑名单 或 删除黑名单 及群名"
        except Exception as e:
            self.logger.warning("写回额度规则失败: %s", e)
            return False, f"写回配置失败: {e}"

    def invalidate_templates_cache(self) -> None:
        """使动态话术模板缓存失效，下次 get_dynamic_templates_config 将重新从文件加载"""
        self._templates_cache = None
        self._templates_mtime = 0

    def invalidate_exchange_rates_cache(self) -> None:
        """使动态汇率配置缓存失效，下次 get_exchange_rates_config 将重新从文件加载"""
        self._exchange_rates_cache = None
        self._exchange_rates_mtime = 0

    def invalidate_strategies_cache(self) -> None:
        """使策略缓存失效，下次 get_strategies_config 将重新从文件加载"""
        self._strategies_cache = None
        self._strategies_mtime = 0

    def get_strategies_config(self) -> Dict[str, Any]:
        """加载 reply_strategies.yaml，mtime 驱动热更新；文件不存在时从 config.yaml 中提取并自动创建"""
        strategies_file = self.config_path.parent / "reply_strategies.yaml"
        if not strategies_file.exists():
            self._bootstrap_strategies_file(strategies_file)
        if not strategies_file.exists():
            return self.config.get("reply_strategies", {}) or {}
        try:
            mtime = os.path.getmtime(strategies_file)
            if self._strategies_cache is not None and mtime <= self._strategies_mtime:
                return self._strategies_cache
            with open(strategies_file, "r", encoding="utf-8") as f:
                self._strategies_cache = yaml.safe_load(f) or {}
            self._strategies_mtime = mtime
            n = len((self._strategies_cache.get("strategies") or {}))
            self.logger.info("回复策略已加载: %d 个策略, 文件=%s", n, strategies_file.name)
            return self._strategies_cache
        except Exception as e:
            self.logger.warning("加载回复策略失败: %s", e)
            return self._strategies_cache if self._strategies_cache is not None else {}

    def _bootstrap_strategies_file(self, path: Path) -> None:
        """从 config.yaml 的 reply_strategies 节提取数据，写入独立 YAML 文件"""
        rs = self.config.get("reply_strategies")
        if not rs or not isinstance(rs, dict):
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(rs, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            self.logger.info("已从 config.yaml 迁移 reply_strategies 到 %s", path.name)
        except Exception as e:
            self.logger.warning("创建 reply_strategies.yaml 失败: %s", e)

    def save_strategies(self, data: Dict[str, Any]) -> tuple:
        """写入 reply_strategies.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self.config_path.parent / "reply_strategies.yaml"
        try:
            tmp = path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_strategies_cache()
            return True, "策略配置已保存"
        except Exception as e:
            self.logger.warning("写回策略配置失败: %s", e)
            if path.with_suffix(".yaml.tmp").exists():
                path.with_suffix(".yaml.tmp").unlink(missing_ok=True)
            return False, f"保存失败: {e}"

    def _get_strategies_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "reply_strategies.yaml"
        return path if path.exists() else None

    def _get_templates_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "templates.yaml"
        return path if path.exists() else None

    def _get_exchange_rates_file_path(self) -> Optional[Path]:
        path = self.config_path.parent / "exchange_rates.yaml"
        return path if path.exists() else None

    def save_templates(self, data: Dict[str, Any]) -> tuple:
        """写入 templates.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self._get_templates_file_path()
        if not path:
            return False, "未找到 templates.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_templates_cache()
            return True, "话术模板已保存"
        except Exception as e:
            self.logger.warning("写回话术模板失败: %s", e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False, f"保存失败: {e}"

    # ── Personas canonical config (personas.yaml) ────────────────────────────

    _personas_cache: Optional[Dict[str, Any]] = None
    _personas_mtime: float = 0

    def get_personas_config(self) -> Dict[str, Any]:
        """加载 personas.yaml（运营人设规范定义），mtime 驱动热更新。
        不存在时返回空 dict（无报警，因为是可选文件）。
        """
        path = self.config_path.parent / "personas.yaml"
        if not path.exists():
            return {}
        try:
            mtime = os.path.getmtime(path)
            if self._personas_cache is not None and mtime <= self._personas_mtime:
                return self._personas_cache
            with open(path, "r", encoding="utf-8") as f:
                self._personas_cache = yaml.safe_load(f) or {}
            self._personas_mtime = mtime
            n = len((self._personas_cache.get("profiles") or {}))
            self.logger.info("personas.yaml 已加载: %d 个 profiles", n)
            return self._personas_cache
        except Exception as e:
            self.logger.warning("加载 personas.yaml 失败: %s", e)
            return self._personas_cache if self._personas_cache is not None else {}

    def save_personas(self, data: Dict[str, Any]) -> tuple:
        """将运营人设写入 personas.yaml（P5-C: atomic write, git-trackable canonical config）。
        data 格式: {"profiles": {pid: persona_dict, ...}, "updated_at": "..."}
        返回 (成功?, 说明)
        """
        path = self.config_path.parent / "personas.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)  # validate before replacing
            # P8-A: rotate previous version to .bak (one-step manual undo)
            bak = path.with_suffix(".yaml.bak")
            if path.exists():
                try:
                    path.replace(bak)
                except Exception:
                    pass
            tmp.replace(path)
            self._personas_cache = None
            self._personas_mtime = 0
            n = len((data.get("profiles") or {}))
            self.logger.info("personas.yaml 已保存: %d 个 profiles", n)
            return True, f"personas.yaml 已保存 ({n} 个 profiles)"
        except Exception as e:
            self.logger.warning("保存 personas.yaml 失败: %s", e)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"保存失败: {e}"

    def get_personas_file_path(self) -> Optional[Path]:
        """返回 personas.yaml 的路径（不论是否存在）。"""
        return self.config_path.parent / "personas.yaml"

    def save_exchange_rates(self, data: Dict[str, Any]) -> tuple:
        """写入 exchange_rates.yaml 并刷新缓存。返回 (成功?, 说明)"""
        path = self._get_exchange_rates_file_path()
        if not path:
            return False, "未找到 exchange_rates.yaml"
        tmp = path.with_suffix(".yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            with open(tmp, "r", encoding="utf-8") as f:
                yaml.safe_load(f)
            tmp.replace(path)
            self.invalidate_exchange_rates_cache()
            return True, "通道配置已保存"
        except Exception as e:
            self.logger.warning("写回通道配置失败: %s", e)
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return False, f"保存失败: {e}"
