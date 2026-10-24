"""pytest 共用夹具：临时目录隔离 + 出网拦截 + 免真连的 RecorderManager。

三处关键设计：

1. 落盘隔离。`recorder.config` 的 SETTINGS_FILE / TARGETS_FILE / DEFAULT_DATA_DIR /
   ENV_FILE 与 `recorder.theme.THEME_FILE`、`recorder.danmaku_style.STYLE_FILE` 都是模块级
   常量，导入时就按项目根目录算好了；但 SettingsStore._load、TargetStore._load 等
   都是调用时才去查这些全局名字，所以 fixture 里换掉它们即可生效，不必改生产代码。
   `danmaku_style._STORE` 是进程内单例，STYLE_FILE 换了必须一并置空，否则仍指旧路径。

2. 不连 B 站。`create_app()` 的 lifespan 会构造 RecorderManager 并 await start()，
   而 start() 会拉起全部房间的录制协程。所以接口测试**不进 TestClient 的 with 块**
   （不进就不跑 lifespan），改为拿到 app 后手动塞 `app.state.mgr`。

3. 不产生系统副作用。打开目录、拉起进程这类动作在路由里走模块级 `_reveal_in_os`，
   这里统一换成记录调用参数的替身，测试只断言参数，不真开窗口。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 让 tests/ 直接跑时也能 import 到项目包（tools/run_tests.ps1 从根目录起 pytest，用不到）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder import config as config_mod          # noqa: E402
from recorder import danmaku_style as ds_mod       # noqa: E402
from recorder import theme as theme_mod            # noqa: E402
from recorder.manager import RecorderManager       # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "tc(*ids): 绑定 test_cases 的 TC 编号，供 tools/backfill_results.py 回填")
    config.addinivalue_line(
        "markers", "live: 依赖真实直播事件，默认 -m 'not live' 跳过")


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """把全部配置文件与数据目录指到 tmp_path，并清掉可能串入的环境凭证。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    data = tmp_path / "data"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config_mod, "SETTINGS_FILE", cfg / "settings.json")
    monkeypatch.setattr(config_mod, "TARGETS_FILE", cfg / "targets.json")
    monkeypatch.setattr(config_mod, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config_mod, "DEFAULT_DATA_DIR", data)
    monkeypatch.setattr(theme_mod, "THEME_FILE", cfg / "report_theme.json")
    monkeypatch.setattr(ds_mod, "STYLE_FILE", cfg / "danmaku_style.json")
    monkeypatch.setattr(ds_mod, "_STORE", None)
    # 本机真实环境里可能带着凭证（.env / 系统变量），清掉避免用例读到
    monkeypatch.delenv("SESSDATA", raising=False)
    monkeypatch.delenv("DANMAKU_ANON_SALT", raising=False)
    return {"config_dir": cfg, "data_dir": data}


@pytest.fixture(autouse=True)
def os_side_effects(monkeypatch):
    """拦掉路由里的系统动作，返回记录用的字典：calls["reveal"] 是最近一次打开的目标。"""
    import server.app as app_mod

    calls: dict = {}

    def _fake_reveal(target: Path) -> None:
        calls["reveal"] = Path(target)

    monkeypatch.setattr(app_mod, "_reveal_in_os", _fake_reveal)
    return calls


@pytest.fixture
def manager(isolated_paths, monkeypatch) -> RecorderManager:
    """真实 RecorderManager，但录制任务换成不连网的替身。

    start_room / stop_room 只改运行态的 running 标记，不创建 record_room 协程，
    这样房间增删改与设置热更新的真实逻辑照跑，而 B 站连接不会发生。
    """
    mgr = RecorderManager()

    async def _fake_start(room_id: int) -> None:
        rt = mgr.runtimes.get(room_id)
        if rt is None:
            raise KeyError(f"房间 {room_id} 不存在")
        rt.enabled = True
        rt.running = True

    async def _fake_stop(room_id: int) -> None:
        rt = mgr.runtimes.get(room_id)
        if rt is not None:
            rt.running = False

    monkeypatch.setattr(mgr, "start_room", _fake_start)
    monkeypatch.setattr(mgr, "stop_room", _fake_stop)
    monkeypatch.setattr(
        mgr, "is_running",
        lambda room_id: bool(mgr.runtimes.get(room_id)
                             and mgr.runtimes[room_id].running))
    return mgr


@pytest.fixture
def client(manager) -> TestClient:
    """未进入 with 块，因此不跑 lifespan、不连 B 站；mgr 由 fixture 手工注入。"""
    from server.app import create_app
    app = create_app()
    app.state.mgr = manager
    c = TestClient(app)
    yield c
    c.close()


@pytest.fixture
def sample_jsonl(isolated_paths):
    """造一份可被真实解析的弹幕记录，供文件类接口（预览 / 分析 / 去重 / 战报）使用。

    行结构严格跟随录制落盘：事件类型键是 `_type`（不是 `type`），首行 session_header
    给战报提供场次标题与主播名，弹幕带 uid_anon / uname_anon / ts，另有 SC 与人气各一条，
    使各统计口径都能取到值，不至于空跑。
    """
    import json

    data_dir: Path = isolated_paths["data_dir"]
    d = data_dir / "records" / "主播甲"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "live_20260101.jsonl"
    rows = [
        {"_type": "session_header", "room_id": 12345, "vup": "主播甲",
         "anchor_name": "主播甲", "title": "测试场次", "live_status": 1,
         "collected_at": "2026-01-01T00:00:00+00:00"},
        {"_type": "danmaku", "text": "第一条弹幕", "uid_anon": "0123456789abcdef",
         "uname_anon": "观众A", "ts": 1735660800.0},
        {"_type": "danmaku", "text": "第一条弹幕", "uid_anon": "0123456789abcdef",
         "uname_anon": "观众A", "ts": 1735660803.0},
        {"_type": "danmaku", "text": "第二条弹幕", "uid_anon": "fedcba9876543210",
         "uname_anon": "观众B", "ts": 1735660809.0},
        {"_type": "super_chat", "text": "挺主播", "uid_anon": "0123456789abcdef",
         "uname_anon": "观众A", "price_rmb": 30, "ts": 1735660815.0},
        {"_type": "popularity", "ts": 1735660816.0, "popularity": 1234},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    return {"path": f, "rel": f"{f.parent.name}/{f.name}", "kind": "records"}
