"""配置持久化：config/settings.json + config/targets.json，.env 提供敏感默认值。

所有写操作走「临时文件 + replace」原子落盘，避免写一半进程退出导致 JSON 损坏。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 打包成绿色版（PyInstaller 冻结）后：
#   ROOT_DIR      = exe 所在目录（可写，config/data/.env 都放这里，用户可见）
#   RESOURCE_DIR  = sys._MEIPASS（只读，存放 web 静态资源）
# 源码运行时两者都是项目根目录。
if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = ROOT_DIR

CONFIG_DIR = ROOT_DIR / "config"
WEB_DIR = RESOURCE_DIR / "web"
DEFAULT_DATA_DIR = ROOT_DIR / "data"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
TARGETS_FILE = CONFIG_DIR / "targets.json"
ENV_FILE = ROOT_DIR / ".env"

_BOOL_FIELDS = ("record_gifts", "filter_pure_emoticon", "record_moderation",
                "anonymize", "sanitize_text", "auto_start", "video_enabled",
                "llm_auto_analyze")
_INT_FIELDS = ("offline_poll_sec", "idle_stop_minutes",
               "video_qn", "video_min_free_gb")
_STR_FIELDS = ("anon_salt", "data_dir", "records_subdir", "weak_subdir",
               "video_subdir")
# AI 配置字符串字段单独成组：不参与目录名校验，仅做存取
_LLM_STR_FIELDS = ("llm_base_url", "llm_api_key", "llm_model", "llm_mode")

# 可选画质档位（qn: B 站直播流清晰度编号）
VIDEO_QN_CHOICES = (10000, 400, 250, 150, 80)
VIDEO_QN_LABELS = {10000: "原画", 400: "蓝光", 250: "超清", 150: "高清", 80: "流畅"}

# AI 高能解读模式：A=轻量解读规则 Top N；C=5 分钟段粗筛补漏+切片文案
LLM_MODES = ("A", "C")
LLM_MODE_LABELS = {"A": "A 轻量解读", "C": "C 深度策划"}


def validate_cared_key(key: str) -> str:
    """校验关心用户标识：纯数字真实 UID 或 16 位十六进制 uid_anon。"""
    key = str(key).strip().lower()
    if key.isdigit():
        return key
    if len(key) == 16 and all(c in "0123456789abcdef" for c in key):
        return key
    raise ValueError("用户标识必须是纯数字 UID 或 16 位十六进制匿名 ID")


def _atomic_write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
    except ImportError:
        pass


@dataclass
class Settings:
    # 凭证
    sessdata: str = ""
    anon_salt: str = "meowmod-default-salt"
    # 存储
    data_dir: str = ""  # 空串=项目内 data/
    # 子目录名可配置：兼容旧版录制器的 raw/weak_label 目录布局
    records_subdir: str = "records"
    weak_subdir: str = "weak_label"
    video_subdir: str = "video"
    # 录制开关
    record_gifts: bool = False           # 礼物事件（默认不录，信息密度低）
    filter_pure_emoticon: bool = True    # 纯大表情弹幕不录（文字+表情保留）
    record_moderation: bool = True       # 房管拉黑/禁言弱标签回捞
    anonymize: bool = True               # uid 哈希 + 用户名掩码
    sanitize_text: bool = True           # 链接/@/QQ/UID/手机号脱敏
    # 视频录制（附加功能，默认关闭；还需房间自身 record_video 开关同时打开）
    video_enabled: bool = False
    video_qn: int = 10000                # 期望画质；游客/无权限时 B 站自动降档
    video_min_free_gb: int = 5           # 磁盘剩余低于该值自动停录并告警
    # AI 高能解读（OpenAI 兼容接口；Key 仅存服务端不下发浏览器）
    llm_base_url: str = ""               # 如 https://api.deepseek.com/v1
    llm_api_key: str = ""
    llm_model: str = ""                  # 如 deepseek-chat / qwen-plus / qwen2.5:14b
    llm_mode: str = "A"                  # 默认分析模式 A/C
    llm_auto_analyze: bool = False       # 场次结束后自动解读（默认手动按钮触发）
    # 调度
    offline_poll_sec: int = 45           # 未开播轮询间隔
    idle_stop_minutes: int = 0           # 全部关播 N 分钟后自动停止（0=不启用）
    auto_start: bool = True              # 服务启动后自动录制 enabled 房间
    # 后处理去重：关心用户名单（全量保留，不去重）
    # key: 纯数字=真实 UID；16 位十六进制=录制文件里的 uid_anon
    cared_users: list[dict] = field(default_factory=list)

    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else DEFAULT_DATA_DIR

    @property
    def records_dir(self) -> Path:
        return self.data_path() / (self.records_subdir.strip() or "records")

    @property
    def weak_dir(self) -> Path:
        return self.data_path() / (self.weak_subdir.strip() or "weak_label")

    @property
    def video_dir(self) -> Path:
        return self.data_path() / (self.video_subdir.strip() or "video")

    def effective_sessdata(self) -> str:
        """环境变量 SESSDATA 优先（兼容 .env / 系统环境变量用法）。"""
        return os.environ.get("SESSDATA", "").strip() or self.sessdata.strip()

    def effective_salt(self) -> str:
        return (os.environ.get("DANMAKU_ANON_SALT", "").strip()
                or self.anon_salt.strip() or "meowmod-default-salt")

    def llm_ready(self) -> bool:
        """AI 解读三项配置是否齐全（Key 允许本地 Ollama 随意填，但字段需非空）。"""
        return bool(self.llm_base_url.strip()
                    and self.llm_api_key.strip()
                    and self.llm_model.strip())

    def public_dict(self) -> dict:
        """给前端的视图：不回传 SESSDATA / LLM Key 明文。"""
        d = asdict(self)
        d["sessdata_set"] = bool(self.effective_sessdata())
        d.pop("sessdata", None)
        d["llm_key_set"] = bool(self.llm_api_key.strip())
        d.pop("llm_api_key", None)
        d["llm_ready"] = self.llm_ready()
        d["data_dir_effective"] = str(self.data_path())
        return d


@dataclass
class Target:
    room_id: int
    vup: str
    enabled: bool = True
    record_video: bool = False        # 该房间是否同时录制直播视频（默认关）
    added_at: float = field(default_factory=lambda: time.time())


class SettingsStore:
    def __init__(self) -> None:
        _load_dotenv()
        self.settings = self._load()

    def _load(self) -> Settings:
        kwargs: dict = {}
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k in (*_BOOL_FIELDS, *_INT_FIELDS, *_STR_FIELDS,
                          *_LLM_STR_FIELDS, "sessdata"):
                    if k in raw:
                        kwargs[k] = raw[k]
            except (json.JSONDecodeError, OSError):
                pass
        # 关心名单做容错清洗（坏数据不影响启动）
        raw_cared = kwargs.pop("cared_users", None)
        if isinstance(raw_cared, list):
            cleaned = []
            seen = set()
            for item in raw_cared:
                if not isinstance(item, dict):
                    continue
                try:
                    key = validate_cared_key(item.get("key", ""))
                except ValueError:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                cleaned.append({
                    "key": key,
                    "note": str(item.get("note", ""))[:60],
                    "added_at": float(item.get("added_at") or time.time()),
                })
            kwargs["cared_users"] = cleaned
        s = Settings(**kwargs)
        # .env / 环境变量只覆盖凭证类，不写回 settings.json
        env_sess = os.environ.get("SESSDATA", "").strip()
        if env_sess:
            s.sessdata = env_sess
        env_salt = os.environ.get("DANMAKU_ANON_SALT", "").strip()
        if env_salt:
            s.anon_salt = env_salt
        return s

    def update(self, patch: dict) -> Settings:
        """合并校验更新。返回更新后的 settings。"""
        s = self.settings
        for k in _BOOL_FIELDS:
            if k in patch:
                setattr(s, k, bool(patch[k]))
        # 子目录名做基础净化：只允许单层目录名，防止注入路径分隔
        _sub_defaults = {"records_subdir": "records", "weak_subdir": "weak_label",
                         "video_subdir": "video"}
        for k in _sub_defaults:
            if patch.get(k) is not None:
                v = str(patch[k]).strip().replace("/", "").replace("\\", "")
                setattr(s, k, v or _sub_defaults[k])
        for k in _INT_FIELDS:
            if patch.get(k) is not None:
                try:
                    v = int(patch[k])
                except (TypeError, ValueError):
                    raise ValueError(f"{k} 必须是整数")
                if v < 0:
                    raise ValueError(f"{k} 不能为负数")
                setattr(s, k, v if v >= 0 else 0)
        for k in ("anon_salt", "data_dir"):
            if patch.get(k) is not None:
                setattr(s, k, str(patch[k]).strip())
        for k in ("records_subdir", "weak_subdir", "video_subdir"):
            if patch.get(k) is not None:
                v = str(patch[k]).strip().replace("\\", "/").strip("/")
                # 只允许单层目录名，防止路径穿越到数据根目录之外
                if not v or "/" in v or v in (".", "..") or ":" in v:
                    raise ValueError(f"{k} 必须是单层目录名（如 raw）")
                setattr(s, k, v)
        if "sessdata" in patch and patch["sessdata"] is not None:
            val = str(patch["sessdata"]).strip()
            # 空串视为「不修改」（表单留空保存）；清空凭证请直接删 settings.json
            if val:
                s.sessdata = val
        # ---- AI 解读配置 ----
        if patch.get("llm_base_url") is not None:
            s.llm_base_url = str(patch["llm_base_url"]).strip().rstrip("/")
        if patch.get("llm_model") is not None:
            s.llm_model = str(patch["llm_model"]).strip()
        if patch.get("llm_api_key") is not None:
            # 空串/纯空格=保持原 Key（与 SESSDATA 相同的交互约定）
            val = str(patch["llm_api_key"]).strip()
            if val:
                s.llm_api_key = val
        if patch.get("llm_mode") is not None:
            m = str(patch["llm_mode"]).strip().upper()
            if m not in LLM_MODES:
                raise ValueError(f"AI 分析模式无效，可选：{LLM_MODES}")
            s.llm_mode = m
        if s.llm_base_url and not re.match(r"^https?://", s.llm_base_url):
            raise ValueError("AI 接口地址必须以 http:// 或 https:// 开头")
        # 输出目录合法性
        p = s.data_path()
        try:
            p.mkdir(parents=True, exist_ok=True)
            test_file = p / ".write_test"
            test_file.write_text("x", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except OSError as e:
            raise ValueError(f"数据目录不可写：{p}（{e}）")
        if s.offline_poll_sec < 10:
            s.offline_poll_sec = 10
        if s.video_qn not in VIDEO_QN_CHOICES:
            raise ValueError(f"画质档位无效，可选：{VIDEO_QN_CHOICES}")
        s.video_min_free_gb = min(max(s.video_min_free_gb, 1), 1000)
        self.save()
        return s

    def save(self) -> None:
        _atomic_write_json(SETTINGS_FILE, asdict(self.settings))

    # ---------- 关心用户名单 ----------
    def list_cared(self) -> list[dict]:
        return list(self.settings.cared_users)

    def add_cared(self, key: str, note: str = "") -> dict:
        key = validate_cared_key(key)
        for item in self.settings.cared_users:
            if item["key"] == key:
                if note:
                    item["note"] = note[:60]
                    self.save()
                    return item
                raise ValueError("该用户已在关心名单中")
        item = {"key": key, "note": note.strip()[:60],
                "added_at": time.time()}
        self.settings.cared_users.append(item)
        self.save()
        return item

    def remove_cared(self, key: str) -> bool:
        key = str(key).strip().lower()
        before = len(self.settings.cared_users)
        self.settings.cared_users = [
            i for i in self.settings.cared_users if i["key"] != key]
        if len(self.settings.cared_users) == before:
            return False
        self.save()
        return True

    def cared_key_set(self) -> set[str]:
        return {i["key"] for i in self.settings.cared_users}


class TargetStore:
    def __init__(self) -> None:
        self.targets: list[Target] = self._load()

    def _load(self) -> list[Target]:
        if not TARGETS_FILE.exists():
            return []
        try:
            raw = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        out: list[Target] = []
        for item in raw.get("targets", []):
            try:
                out.append(Target(
                    room_id=int(item["room_id"]),
                    vup=str(item.get("vup") or f"room_{item['room_id']}").strip(),
                    enabled=bool(item.get("enabled", True)),
                    record_video=bool(item.get("record_video", False)),
                    added_at=float(item.get("added_at") or time.time()),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def save(self) -> None:
        _atomic_write_json(TARGETS_FILE, {
            "_说明": "录制目标（也可在 Web 界面「房间管理」中维护）。"
                    "room_id=直播间URL中的数字；vup=保存目录名。",
            "targets": [asdict(t) for t in self.targets],
        })

    def get(self, room_id: int) -> Target | None:
        return next((t for t in self.targets if t.room_id == room_id), None)

    def add(self, room_id: int, vup: str, enabled: bool = True,
            record_video: bool = False) -> Target:
        if self.get(room_id):
            raise ValueError(f"房间 {room_id} 已存在")
        t = Target(room_id=room_id, vup=vup or f"room_{room_id}",
                   enabled=enabled, record_video=record_video)
        self.targets.append(t)
        self.save()
        return t

    def bulk_add(self, pairs: list[tuple[int, str]],
                 enabled: bool = True,
                 record_video: bool = False
                 ) -> tuple[list[Target], list[int]]:
        """批量加入房间（单次落盘）。返回 (新增目标, 已存在被跳过的 room_id)。"""
        new: list[Target] = []
        skipped: list[int] = []
        for room_id, vup in pairs:
            if self.get(room_id):
                skipped.append(room_id)
                continue
            t = Target(room_id=room_id,
                       vup=(vup or "").strip() or f"room_{room_id}",
                       enabled=enabled, record_video=record_video)
            self.targets.append(t)
            new.append(t)
        if new:
            self.save()
        return new, skipped

    def update(self, room_id: int, vup: str | None = None,
               enabled: bool | None = None,
               record_video: bool | None = None) -> Target:
        t = self.get(room_id)
        if not t:
            raise KeyError(f"房间 {room_id} 不存在")
        if vup is not None:
            t.vup = vup.strip() or f"room_{room_id}"
        if enabled is not None:
            t.enabled = bool(enabled)
        if record_video is not None:
            t.record_video = bool(record_video)
        self.save()
        return t

    def remove(self, room_id: int) -> None:
        self.targets = [t for t in self.targets if t.room_id != room_id]
        self.save()
