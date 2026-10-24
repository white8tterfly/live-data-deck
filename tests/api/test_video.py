"""视频直录区块（/api/videos）后端接口测试。

覆盖：ffmpeg 探测、场次列表、打开目录、高能检测、ECG 聚合与 seek、AI 结果读写、
导出弹幕版、秒切、重生成字幕、下载、删除场次 / 散片，以及按路径取文件的安全边界。

全部用例不起服务、不出网：ffmpeg 探测用替身换掉，AI 调用 patch 掉 _chat，
视频「造场次」直接写 meta.json + 假 FLV，读的是服务端配置派生的真实临时目录。
"""
from __future__ import annotations

import json
import re

import pytest

from recorder import llm_analyze as llm_mod
from recorder import media as media_mod

VUP = "主播甲"
SESSION = "live_20260101_1200"
SEG = "live_20260101_1200.flv"
JSONL = "live_20260101.jsonl"
T0 = 1_704_099_000.0  # 60 的整数倍，保证弹幕桶起点与场次起点对齐


def _no_ffmpeg(monkeypatch) -> None:
    """把 ffmpeg 探测换成「未安装」，避免触达本机 ffmpeg。"""
    monkeypatch.setattr(media_mod, "find_ffmpeg", lambda: None)


def make_session(manager, *, vup: str = VUP, base: str = SESSION,
                 seg: str = SEG, start_ts: float = T0, duration: float = 1800.0,
                 ass: bool = False) -> dict:
    """在视频根目录造一个已收尾的场次（FLV + meta.json），返回关键路径与内容。"""
    vdir = manager.settings.video_path() / vup
    vdir.mkdir(parents=True, exist_ok=True)
    seg_path = vdir / seg
    seg_path.write_bytes(b"FLV\x01" + b"\x00" * 512)
    meta = {
        "_type": "video_session", "version": 1, "room_id": 123456,
        "vup": vup, "anchor_name": "主播", "title": "测试场次",
        "base": base, "start_ts": start_ts,
        "start_clock": "2026-01-01 12:00:00",
        "end_ts": start_ts + duration,
        "qn": 10000, "qn_label": "原画", "requested_qn": 10000,
        "protocol": "http_stream", "format": "flv", "codec": "avc",
        "jsonl": JSONL,
        "segments": [{"file": seg, "start_ts": start_ts,
                      "end_ts": start_ts + duration, "discarded": False}],
        "ass": None,
    }
    if ass:
        meta["ass"] = f"{base}.ass"
        (vdir / f"{base}.ass").write_text("[Script Info]\n", encoding="utf-8")
    meta_path = vdir / f"{base}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"vdir": vdir, "meta": meta, "meta_path": meta_path,
            "seg_path": seg_path, "base": base, "vup": vup}


def write_jsonl(manager, rows: list[dict], *, vup: str = VUP,
                name: str = JSONL):
    """写一份弹幕 jsonl 到该主播的 records 目录。"""
    d = manager.settings.records_dir / vup
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    return p


def dm(ts: float, i: int, text: str | None = None) -> dict:
    """造一条弹幕记录（uid_anon 用 16 位十六进制，符合匿名格式）。"""
    return {"_type": "danmaku", "ts": ts, "uid_anon": f"{i:016x}",
            "text": text or f"弹幕{i}", "uname_anon": "张***"}


def set_llm(manager) -> None:
    """把 AI 三项配置填齐，让 llm_ready() 通过（不触网）。"""
    s = manager.settings
    s.llm_base_url = "http://127.0.0.1:1/v1"
    s.llm_api_key = "test-key"
    s.llm_model = "test-model"
    s.llm_mode = "A"


# ffmpeg 探测

def test_ffmpeg_available(client, monkeypatch):
    """find_ffmpeg 命中时 /api/videos/ffmpeg 返回 available=true 及路径版本。"""
    monkeypatch.setattr(media_mod, "find_ffmpeg",
                        lambda: {"path": "C:/fake/ffmpeg.exe", "version": "7.0"})
    r = client.get("/api/videos/ffmpeg")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["path"].endswith("ffmpeg.exe")
    assert body["version"] == "7.0"


def test_ffmpeg_missing(client, monkeypatch):
    """find_ffmpeg 未命中时 /api/videos/ffmpeg 返回 available=false，不抛异常。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/ffmpeg")
    assert r.status_code == 200
    assert r.json()["available"] is False


# 场次列表

def test_list_videos_empty(client, manager, monkeypatch):
    """无任何视频数据时列表接口 200，sessions 与 orphans 均为空。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert body["orphans"] == []
    assert "dir" in body and body["ffmpeg"] is None


def test_list_videos_with_session(client, manager, monkeypatch):
    """造一个已收尾场次后，列表能列出该场次并含 segments 与 bytes 统计。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.get("/api/videos")
    assert r.status_code == 200
    sessions = r.json()["sessions"]
    assert len(sessions) == 1
    s = sessions[0]
    assert s["vup"] == VUP and s["base"] == SESSION
    assert s["segment_count"] == 1
    assert s["bytes"] > 0
    assert s["segments"][0]["file"] == SEG
    assert s["segments"][0]["exists"] is True


def test_list_videos_corrupt_meta_no_500(client, manager, monkeypatch):
    """meta.json 内容损坏时列表接口不 500，该场次被跳过、残段落入散片。"""
    _no_ffmpeg(monkeypatch)
    vdir = manager.settings.video_path() / VUP
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / SEG).write_bytes(b"FLV\x01" + b"\x00" * 64)
    (vdir / f"{SESSION}.meta.json").write_text("{ not json", encoding="utf-8")
    r = client.get("/api/videos")
    assert r.status_code == 200
    body = r.json()
    assert body["sessions"] == []
    assert any(o["file"] == SEG for o in body["orphans"])


# 打开视频目录

@pytest.mark.tc("TC-VID-B05")
def test_open_video_folder_reveals_video_dir(client, manager, os_side_effects):
    """打开视频目录接口 200，reveal 目标解析后落在视频根目录内。"""
    r = client.post("/api/videos/open-folder")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    reveal = os_side_effects["reveal"]
    root = manager.settings.video_path().resolve()
    assert reveal == root or root in reveal.parents
    from pathlib import Path
    assert Path(r.json()["path"]) == root


# 高能检测

def test_highlights_session_missing_404(client, manager, monkeypatch):
    """场次 meta 不存在时高能检测接口 404。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/highlights",
                   params={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


def test_highlights_normal(client, manager, monkeypatch):
    """有 meta 与密集弹幕时高能接口 200，返回窗口列表且带分段 seek 定位。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    write_jsonl(manager, [dm(T0 + i, i) for i in range(12)])
    r = client.get("/api/videos/highlights",
                   params={"vup": VUP, "session": SESSION,
                           "window_sec": 60, "top_n": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["jsonl_exists"] is True
    assert body["ffmpeg"]["available"] is False
    assert isinstance(body["windows"], list) and body["windows"]
    assert body["windows"][0]["seek"]["part"] == SEG


# 直播心电图

def test_ecg_normal(client, manager, monkeypatch):
    """有 meta 与弹幕数据的场次 ECG 聚合 200，返回 series 与分段清单。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    write_jsonl(manager, [dm(T0 + i, i) for i in range(5)])
    r = client.get("/api/videos/ecg", params={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    body = r.json()
    assert body["vup"] == VUP and body["session"] == SESSION
    assert isinstance(body["series"], list) and body["series"]
    assert len(body["segments"]) == 1
    assert body["jsonl_exists"] is True


@pytest.mark.tc("TC-ECG-E07")
def test_ecg_invalid_session_400(client, manager, monkeypatch):
    """场次名不符合 live_YYYYMMDD_HHMM 格式时 ECG 接口 400。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/ecg", params={"vup": VUP, "session": "../etc"})
    assert r.status_code == 400


def test_ecg_bucket_out_of_range_400(client, manager, monkeypatch):
    """bucket 超出 10–600 秒范围时 ECG 接口 400。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.get("/api/videos/ecg",
                   params={"vup": VUP, "session": SESSION, "bucket": "5000"})
    assert r.status_code == 400


def test_ecg_missing_meta_404(client, manager, monkeypatch):
    """场次 meta 缺失时 ECG 接口 404。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/ecg", params={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


def test_ecg_seek_normal(client, manager, monkeypatch):
    """ecg-seek 正常路径：按 ts 定位到分段并返回跳转命令。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.post("/api/videos/ecg-seek",
                    json={"vup": VUP, "session": SESSION, "ts": T0 + 100})
    assert r.status_code == 200
    body = r.json()
    assert body["part"] == SEG
    assert body["start_sec"] >= 0
    assert "commands" in body


def test_ecg_seek_illegal_body_422(client, manager, monkeypatch):
    """ecg-seek 缺少必填参数时 pydantic 校验返回 422。"""
    _no_ffmpeg(monkeypatch)
    r = client.post("/api/videos/ecg-seek", json={"vup": VUP})
    assert r.status_code == 422


# AI 结果与解读

def test_ai_result_no_cache_404(client, manager, monkeypatch):
    """从未跑过 AI 的场次查询结果接口返回 404。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.get("/api/videos/ai-result",
                   params={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


@pytest.mark.tc("TC-AI-I02")
def test_ai_result_cached(client, manager, monkeypatch):
    """已写入 ai.json 缓存的场次直接返回缓存结果，不触发模型调用。"""
    _no_ffmpeg(monkeypatch)
    info = make_session(manager)
    cache = {"_type": "ai_highlights", "mode": "A", "summary": "一句话总结",
             "picks": [], "extra_windows": []}
    (info["vdir"] / f"{SESSION}.ai.json").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    r = client.get("/api/videos/ai-result",
                   params={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "一句话总结"
    assert body["ffmpeg_available"] is False


@pytest.mark.tc("TC-AI-E01")
def test_ai_analyze_not_configured_400(client, manager, monkeypatch):
    """未配置 AI 三项时解读接口 400 且给出「先配置」提示，不发模型请求。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    s = manager.settings
    s.llm_base_url = s.llm_api_key = s.llm_model = ""
    r = client.post("/api/videos/ai-analyze",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 400
    assert "AI" in r.json()["detail"]


def test_ai_analyze_session_missing_404(client, manager, monkeypatch):
    """AI 已配置但场次 meta 缺失时解读接口 404。"""
    _no_ffmpeg(monkeypatch)
    set_llm(manager)
    r = client.post("/api/videos/ai-analyze",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


def test_ai_analyze_normal(client, manager, monkeypatch):
    """patch 掉 _chat 后 A 模式解读 200，返回 picks 并把结果写入 ai.json 缓存。"""
    _no_ffmpeg(monkeypatch)
    set_llm(manager)
    info = make_session(manager)
    write_jsonl(manager, [dm(T0 + i, i) for i in range(12)])

    async def fake_chat(base_url, api_key, model, messages, **kwargs):
        # 从 user 消息里取规则窗口的 start_ts，回一个能吸附上的 pick
        text = messages[-1]["content"]
        m = re.search(r'"start_ts":\s*(\d+)', text)
        start = int(m.group(1))
        return json.dumps({
            "summary": "整场总结",
            "picks": [{"start": start, "title": "切片标题", "cover": "封面",
                       "reason": "高能", "category": "搞笑",
                       "pad_before": 5, "pad_after": 5}],
        }, ensure_ascii=False)

    monkeypatch.setattr(llm_mod, "_chat", fake_chat)
    r = client.post("/api/videos/ai-analyze",
                    json={"vup": VUP, "session": SESSION, "mode": "A"})
    assert r.status_code == 200
    body = r.json()
    assert body["_type"] == "ai_highlights"
    assert body["picks"] and body["picks"][0]["title"] == "切片标题"
    assert (info["vdir"] / f"{SESSION}.ai.json").is_file()


# 导出弹幕版

@pytest.mark.tc("TC-VID-E05")
def test_export_sub_no_ffmpeg_400(client, manager, monkeypatch):
    """无 ffmpeg 时导出弹幕版返回 400 明确提示，而非 500。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager, ass=True)
    r = client.post("/api/videos/export-sub",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 400
    assert "ffmpeg" in r.json()["detail"]


def test_export_sub_starts_job(client, manager, monkeypatch):
    """有 ffmpeg（替身）且场次已收尾时，导出接口启动后台任务并返回 running。"""
    make_session(manager, ass=True)
    monkeypatch.setattr(media_mod, "find_ffmpeg",
                        lambda: {"path": "ff", "version": "x"})
    monkeypatch.setattr(media_mod, "burn_subtitle",
                        lambda ff, src, ass, dst, dur=None, cb=None:
                        {"file": dst.name, "size": 10})
    monkeypatch.setattr(media_mod, "probe_duration", lambda ff, p: 10.0)
    r = client.post("/api/videos/export-sub",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "running"
    assert body["outputs"] == ["live_20260101_1200.danmaku.mp4"]
    # 进程内任务表已登记，查询进度不再是 none
    r2 = client.get("/api/videos/export-sub",
                    params={"vup": VUP, "session": SESSION})
    assert r2.json()["status"] in {"running", "done", "error"}


def test_export_sub_status_none(client, manager, monkeypatch):
    """未发起导出的场次查询进度返回 status=none。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/export-sub",
                   params={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    assert r.json() == {"status": "none"}


# 秒切

def test_cut_session_missing_404(client, manager, monkeypatch):
    """场次 meta 缺失时切片接口 404。"""
    _no_ffmpeg(monkeypatch)
    r = client.post("/api/videos/cut",
                    json={"vup": VUP, "session": SESSION, "part": SEG,
                          "start_sec": 0, "end_sec": 10})
    assert r.status_code == 404


def test_cut_segment_not_in_session_400(client, manager, monkeypatch):
    """分段不属于本场次时切片接口 400。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.post("/api/videos/cut",
                    json={"vup": VUP, "session": SESSION, "part": "other.flv",
                          "start_sec": 0, "end_sec": 10})
    assert r.status_code == 400


def test_cut_end_not_after_start_400(client, manager, monkeypatch):
    """结束秒数不大于起始秒数时切片接口 400。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.post("/api/videos/cut",
                    json={"vup": VUP, "session": SESSION, "part": SEG,
                          "start_sec": 30, "end_sec": 10})
    assert r.status_code == 400


def test_cut_normal(client, manager, monkeypatch):
    """patch 掉 cut_clip 后对已收尾分段切片返回 200 与产物文件名。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    monkeypatch.setattr(media_mod, "cut_clip",
                        lambda src, dst, start, dur:
                        {"file": dst.name, "size": 8, "cmd": "ffmpeg ..."})
    r = client.post("/api/videos/cut",
                    json={"vup": VUP, "session": SESSION, "part": SEG,
                          "start_sec": 10, "end_sec": 40})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["file"] == f"{SESSION}.clip_10.flv"


# 重生成字幕

def test_regenerate_ass_session_missing_404(client, manager, monkeypatch):
    """场次 meta 缺失时重生成字幕接口 404。"""
    _no_ffmpeg(monkeypatch)
    r = client.post("/api/videos/regenerate-ass",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


@pytest.mark.tc("TC-DSR-B03")
def test_regenerate_ass_normal(client, manager, monkeypatch):
    """有弹幕数据时重新生成 ASS 字幕返回 200，写盘并回写 meta。"""
    _no_ffmpeg(monkeypatch)
    info = make_session(manager)
    write_jsonl(manager, [dm(T0 + i, i) for i in range(5)])
    r = client.post("/api/videos/regenerate-ass",
                    json={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ass"] == f"{SESSION}.ass"
    assert (info["vdir"] / f"{SESSION}.ass").is_file()


# 下载

def test_download_normal(client, manager, monkeypatch):
    """下载场次产物：合法 FLV 文件返回 200 且内容正确。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.get("/api/videos/download",
                   params={"vup": VUP, "file": SEG})
    assert r.status_code == 200
    assert r.content[:4] == b"FLV\x01"


def test_download_traversal_403(client, manager, monkeypatch):
    """越界路径（..）被拒绝返回 403，不读任意文件。"""
    _no_ffmpeg(monkeypatch)
    make_session(manager)
    r = client.get("/api/videos/download",
                   params={"vup": VUP, "file": "../../secret.flv"})
    assert r.status_code == 403


def test_download_bad_suffix_400(client, manager, monkeypatch):
    """后缀不在 FLV / ASS / MP4 之内时下载接口 400。"""
    _no_ffmpeg(monkeypatch)
    info = make_session(manager)
    (info["vdir"] / "note.txt").write_text("x", encoding="utf-8")
    r = client.get("/api/videos/download",
                   params={"vup": VUP, "file": "note.txt"})
    assert r.status_code == 400


def test_download_vup_traversal_403(client, manager, monkeypatch):
    """主播目录越界（vup=..）返回 403。"""
    _no_ffmpeg(monkeypatch)
    r = client.get("/api/videos/download",
                   params={"vup": "..", "file": "x.flv"})
    assert r.status_code == 403


# 删除场次 / 散片

def test_delete_video_missing_404(client, manager, monkeypatch):
    """删除不存在的场次返回 404。"""
    _no_ffmpeg(monkeypatch)
    r = client.delete("/api/videos",
                      params={"vup": VUP, "session": SESSION})
    assert r.status_code == 404


@pytest.mark.tc("TC-VID-B08")
def test_delete_video_normal(client, manager, monkeypatch):
    """删除已收尾场次：分段 / 字幕 / meta 一并删除并返回 removed 列表。"""
    _no_ffmpeg(monkeypatch)
    info = make_session(manager, ass=True)
    r = client.delete("/api/videos",
                      params={"vup": VUP, "session": SESSION})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert f"{SESSION}.meta.json" in body["removed"]
    assert not (info["vdir"] / f"{SESSION}.meta.json").exists()
    assert not (info["vdir"] / SEG).exists()


def test_delete_video_file_normal(client, manager, monkeypatch):
    """删除散片 FLV 返回 200 且文件消失。"""
    _no_ffmpeg(monkeypatch)
    vdir = manager.settings.video_path() / VUP
    vdir.mkdir(parents=True, exist_ok=True)
    f = vdir / "orphan.flv"
    f.write_bytes(b"FLV\x01")
    r = client.delete("/api/videos/file",
                      params={"vup": VUP, "file": "orphan.flv"})
    assert r.status_code == 200
    assert not f.exists()


def test_delete_video_file_missing_404(client, manager, monkeypatch):
    """删除不存在的散片返回 404。"""
    _no_ffmpeg(monkeypatch)
    vdir = manager.settings.video_path() / VUP
    vdir.mkdir(parents=True, exist_ok=True)
    r = client.delete("/api/videos/file",
                      params={"vup": VUP, "file": "nope.flv"})
    assert r.status_code == 404


def test_delete_video_file_traversal_403(client, manager, monkeypatch):
    """删除散片时越界路径（..）返回 403。"""
    _no_ffmpeg(monkeypatch)
    vdir = manager.settings.video_path() / VUP
    vdir.mkdir(parents=True, exist_ok=True)
    r = client.delete("/api/videos/file",
                      params={"vup": VUP, "file": "../../x.flv"})
    assert r.status_code == 403
