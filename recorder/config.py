"""配置持久化：config/settings.json + config/targets.json，.env 提供敏感默认值。

所有写操作走「临时文件 + replace」原子落盘，避免写一半进程退出导致 JSON 损坏。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
WEB_DIR = ROOT_DIR / "web"
DEFAULT_DATA_DIR = ROOT_DIR / "data"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
TARGETS_FILE = CONFIG_DIR / "targets.json"
ENV_FILE = ROOT_DIR / ".env"

_BOOL_FIELDS = ("record_gifts", "filter_pure_emoticon", "record_moderation",
                "anonymize", "sanitize_text", "auto_start")
_INT_FIELDS = ("offline_poll_sec", "idle_stop_minutes")
_STR_FIELDS = ("anon_salt", "data_dir")


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
    # 录制开关
    record_gifts: bool = False           # 礼物事件（默认不录，信息密度低）
    filter_pure_emoticon: bool = True    # 纯大表情弹幕不录（文字+表情保留）
    record_moderation: bool = True       # 房管拉黑/禁言弱标签回捞
    anonymize: bool = True               # uid 哈希 + 用户名掩码
    sanitize_text: bool = True           # 链接/@/QQ/UID/手机号脱敏
    # 调度
    offline_poll_sec: int = 45           # 未开播轮询间隔
    idle_stop_minutes: int = 0           # 全部关播 N 分钟后自动停止（0=不启用）
    auto_start: bool = True              # 服务启动后自动录制 enabled 房间

    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else DEFAULT_DATA_DIR

    @property
    def records_dir(self) -> Path:
        return self.data_path() / "records"

    @property
    def weak_dir(self) -> Path:
        return self.data_path() / "weak_label"

    def effective_sessdata(self) -> str:
        """环境变量 SESSDATA 优先（兼容 .env / 系统环境变量用法）。"""
        return os.environ.get("SESSDATA", "").strip() or self.sessdata.strip()

    def effective_salt(self) -> str:
        return (os.environ.get("DANMAKU_ANON_SALT", "").strip()
                or self.anon_salt.strip() or "meowmod-default-salt")

    def public_dict(self) -> dict:
        """给前端的视图：不回传 SESSDATA 明文。"""
        d = asdict(self)
        d["sessdata_set"] = bool(self.effective_sessdata())
        d.pop("sessdata", None)
        d["data_dir_effective"] = str(self.data_path())
        return d


@dataclass
class Target:
    room_id: int
    vup: str
    enabled: bool = True
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
                for k in (*_BOOL_FIELDS, *_INT_FIELDS, *_STR_FIELDS, "sessdata"):
                    if k in raw:
                        kwargs[k] = raw[k]
            except (json.JSONDecodeError, OSError):
                pass
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
        for k in _INT_FIELDS:
            if patch.get(k) is not None:
                try:
                    v = int(patch[k])
                except (TypeError, ValueError):
                    raise ValueError(f"{k} 必须是整数")
                if v < 0:
                    raise ValueError(f"{k} 不能为负数")
                setattr(s, k, v if v >= 0 else 0)
        for k in _STR_FIELDS:
            if patch.get(k) is not None:
                setattr(s, k, str(patch[k]).strip())
        if "sessdata" in patch and patch["sessdata"] is not None:
            val = str(patch["sessdata"]).strip()
            # 空串视为「不修改」（表单留空保存）；清空凭证请直接删 settings.json
            if val:
                s.sessdata = val
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
        self.save()
        return s

    def save(self) -> None:
        _atomic_write_json(SETTINGS_FILE, asdict(self.settings))


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

    def add(self, room_id: int, vup: str, enabled: bool = True) -> Target:
        if self.get(room_id):
            raise ValueError(f"房间 {room_id} 已存在")
        t = Target(room_id=room_id, vup=vup or f"room_{room_id}", enabled=enabled)
        self.targets.append(t)
        self.save()
        return t

    def update(self, room_id: int, vup: str | None = None,
               enabled: bool | None = None) -> Target:
        t = self.get(room_id)
        if not t:
            raise KeyError(f"房间 {room_id} 不存在")
        if vup is not None:
            t.vup = vup.strip() or f"room_{room_id}"
        if enabled is not None:
            t.enabled = bool(enabled)
        self.save()
        return t

    def remove(self, room_id: int) -> None:
        self.targets = [t for t in self.targets if t.room_id != room_id]
        self.save()
