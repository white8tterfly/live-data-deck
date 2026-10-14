"""FastAPI 服务：REST API + WebSocket 实时推送 + 静态 Web UI 托管。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from recorder import analysis as analysis_mod
from recorder import dedup as dedup_mod
from recorder import highlights as highlights_mod
from recorder import llm_analyze as llm_mod
from recorder import media as media_mod
from recorder import report as report_mod
from recorder import video_recorder as vr_mod
from recorder.config import LLM_MODES, WEB_DIR
from recorder.manager import RecorderManager
from recorder.session import verify_sessdata
from recorder.theme import (CARD_META, CARD_COLS_CHOICES, COLOR_META,
                            FONT_SCALES, RANK_META, SECTION_META,
                            ReportTheme, WIDTH_CHOICES, coerce_theme)
from recorder import following as following_mod
from server.schemas import (AnalysisIn, CaredUserIn, CookieTestIn, DedupBatchIn,
                            DedupIn, LlmTestIn, ReportIn, TargetIn, TargetPatch,
                            TargetsBulkIn, VideoAiIn, VideoCutIn,
                            VideoSessionIn)

PREVIEW_BYTES = 256 * 1024   # 大文件只从尾部读 256KB
PREVIEW_MAX_LINES = 500


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        mgr = RecorderManager()
        await mgr.start()
        app.state.mgr = mgr
        try:
            yield
        finally:
            await mgr.shutdown()

    app = FastAPI(title="B站直播弹幕录制器", version="1.4.0", lifespan=lifespan)

    def mgr() -> RecorderManager:
        return app.state.mgr

    # ---------------- 状态 ----------------
    @app.get("/api/state")
    async def get_state():
        return mgr().snapshot()

    @app.get("/api/logs")
    async def get_logs(limit: int = Query(300, ge=1, le=1000)):
        return {"logs": mgr().bus.recent_logs(limit)}

    # ---------------- 设置 ----------------
    @app.put("/api/settings")
    async def put_settings(patch: dict):
        try:
            s = await mgr().apply_settings(patch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return s.public_dict()

    @app.post("/api/settings/test-cookie")
    async def test_cookie(body: CookieTestIn):
        sessdata = body.sessdata
        if sessdata is None:
            sessdata = mgr().settings.effective_sessdata()
        if not sessdata.strip():
            return {"ok": False, "message": "未填写 SESSDATA"}
        return await asyncio.to_thread(verify_sessdata, sessdata)

    @app.post("/api/settings/test-llm")
    async def test_llm(body: LlmTestIn):
        s = mgr().settings
        key = (body.api_key or "").strip() or s.llm_api_key
        if not key.strip():
            return {"ok": False, "message": "未填写 API Key"}
        try:
            return await llm_mod.test_connection(
                base_url=body.base_url, api_key=key, model=body.model)
        except llm_mod.LLMConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except llm_mod.LLMRuntimeError as e:
            return {"ok": False, "message": str(e)}

    # ---------------- 房间管理 ----------------
    @app.get("/api/targets")
    async def list_targets():
        return {"targets": [
            {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled,
             "record_video": t.record_video, "added_at": t.added_at}
            for t in mgr().target_store.targets]}

    @app.post("/api/targets", status_code=201)
    async def add_target(body: TargetIn):
        try:
            t = await mgr().add_target(
                body.room_id, body.vup.strip(), body.enabled,
                record_video=body.record_video)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled,
                "record_video": t.record_video}

    @app.put("/api/targets/{room_id}")
    async def update_target(room_id: int, body: TargetPatch):
        try:
            t = await mgr().update_target(
                room_id, body.vup, body.enabled,
                record_video=body.record_video)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled,
                "record_video": t.record_video}

    @app.delete("/api/targets/{room_id}")
    async def delete_target(room_id: int):
        if room_id not in mgr().runtimes:
            raise HTTPException(status_code=404, detail="房间不存在")
        await mgr().remove_target(room_id)
        return {"ok": True}

    @app.post("/api/targets/bulk", status_code=201)
    async def add_targets_bulk(body: TargetsBulkIn):
        """从关注列表批量导入：跳过已存在，新增房间错峰启动。"""
        pairs = [(i.room_id, i.vup.strip()) for i in body.items]
        new, skipped, started = await mgr().add_targets_bulk(
            pairs, body.enabled)
        return {
            "added": [{"room_id": t.room_id, "vup": t.vup} for t in new],
            "skipped": skipped,
            "started": started,
        }

    # ---------------- B 站关注列表（需登录态） ----------------
    @app.get("/api/followings")
    async def list_followings():
        sd = mgr().settings.effective_sessdata()
        if not sd.strip():
            raise HTTPException(
                status_code=400,
                detail="尚未配置 SESSDATA：关注列表属于账号私有数据，"
                       "游客模式无法读取，请先到「设置」页填写登录凭证")
        try:
            items = await asyncio.to_thread(
                following_mod.fetch_all_followed, sd)
        except following_mod.FollowingError as e:
            raise HTTPException(
                status_code=401 if e.code in (-101, -111) else 502,
                detail=str(e))
        existing = {t.room_id: t for t in mgr().target_store.targets}
        out = []
        for it in items:
            t = existing.get(it["room_id"])
            out.append({**it, "added": t is not None,
                        "enabled": bool(t.enabled) if t else False})
        return {
            "total": len(out),
            "live_count": sum(1 for x in out if x["live_status"] == 1),
            "items": out,
        }

    # ---------------- 录制控制 ----------------
    @app.post("/api/rooms/{room_id}/start")
    async def start_room(room_id: int):
        if room_id not in mgr().runtimes:
            raise HTTPException(status_code=404, detail="房间不存在")
        await mgr().start_room(room_id)
        return {"ok": True}

    @app.post("/api/rooms/{room_id}/stop")
    async def stop_room(room_id: int):
        await mgr().stop_room(room_id)
        return {"ok": True}

    @app.post("/api/start-all")
    async def start_all():
        started = await mgr().start_all()
        return {"ok": True, "started": started}

    @app.post("/api/stop-all")
    async def stop_all():
        await mgr().stop_all()
        return {"ok": True}

    # ---------------- 录制文件 ----------------
    def _base_dir(kind: str) -> Path:
        if kind == "records":
            return mgr().settings.records_dir
        if kind == "weak":
            return mgr().settings.weak_dir
        raise HTTPException(status_code=400, detail="kind 必须是 records 或 weak")

    def _safe_file(kind: str, rel: str) -> Path:
        base = _base_dir(kind).resolve()
        p = (base / rel).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="非法路径")
        if not p.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return p

    @app.get("/api/files")
    async def list_files():
        result = {}
        now = time.time()
        for kind, base in (("records", mgr().settings.records_dir),
                           ("weak", mgr().settings.weak_dir)):
            items = []
            if base.exists():
                for f in sorted(base.rglob("*.jsonl")):
                    try:
                        stat = f.stat()
                        items.append({
                            "vup": f.parent.name,
                            "name": f.name,
                            "path": str(f.relative_to(base)),
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "age_hour": round((now - stat.st_mtime) / 3600, 1),
                            "deduped": f.name.endswith(".dedup.jsonl"),
                        })
                    except OSError:
                        continue
            result[kind] = sorted(items, key=lambda x: x["mtime"], reverse=True)
        return result

    @app.get("/api/files/preview")
    async def preview_file(kind: str, path: str,
                           limit: int = Query(200, ge=1, le=PREVIEW_MAX_LINES)):
        p = _safe_file(kind, path)
        try:
            if p.stat().st_size <= PREVIEW_BYTES:
                text = p.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
            else:
                with p.open("rb") as f:
                    f.seek(-PREVIEW_BYTES, 2)
                    chunk = f.read().decode("utf-8", errors="replace")
                lines = chunk.splitlines()[1:]  # 丢掉首行可能截断的半行
        except OSError as e:
            raise HTTPException(status_code=500, detail=str(e))
        return {"path": path, "truncated": len(lines) > limit,
                "lines": lines[-limit:]}

    @app.get("/api/files/download")
    async def download_file(kind: str, path: str):
        p = _safe_file(kind, path)
        return FileResponse(p, filename=p.name, media_type="application/x-jsonlines")

    # ---------------- 关心用户名单 ----------------
    @app.get("/api/cared-users")
    async def list_cared():
        return {"cared_users": mgr().settings_store.list_cared()}

    @app.post("/api/cared-users", status_code=201)
    async def add_cared(body: CaredUserIn):
        try:
            item = mgr().settings_store.add_cared(body.key, body.note)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        mgr().settings = mgr().settings_store.settings
        return item

    @app.delete("/api/cared-users/{key}")
    async def remove_cared(key: str):
        if not mgr().settings_store.remove_cared(key):
            raise HTTPException(status_code=404, detail="名单中无此用户")
        mgr().settings = mgr().settings_store.settings
        return {"ok": True}

    # ---------------- 数据分析 ----------------
    @app.post("/api/analysis")
    async def run_analysis(body: AnalysisIn):
        files = [_safe_file(body.kind, rel) for rel in body.paths]
        try:
            return await asyncio.to_thread(analysis_mod.analyze_files, files)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    # ---------------- 后处理去重 ----------------
    def _do_dedup(rel: str, kind: str, window_sec: int, write: bool) -> dict:
        src = _safe_file(kind, rel)
        if src.name.endswith(".dedup.jsonl"):
            raise HTTPException(status_code=400,
                                detail="该文件已是去重结果，无需再次去重")
        cared = mgr().settings_store.cared_key_set()
        if write:
            return dedup_mod.run_dedup(src, cared, window_sec)
        return dedup_mod.preview_dedup(src, cared, window_sec)

    @app.post("/api/dedup/preview")
    async def dedup_preview(body: DedupIn):
        return await asyncio.to_thread(
            _do_dedup, body.path, body.kind, body.window_sec, False)

    @app.post("/api/dedup/run")
    async def dedup_run(body: DedupIn):
        return await asyncio.to_thread(
            _do_dedup, body.path, body.kind, body.window_sec, True)

    # ---------------- 单场直播战报 ----------------
    def _theme_payload():
        store = mgr().theme_store
        return {
            "theme": store.theme.public_dict(),
            "defaults": ReportTheme().public_dict(),
            "presets": store.presets(),
            "meta": {
                "colors": [{"key": k, "label": l} for k, l in COLOR_META],
                "sections": [{"key": k, "label": l} for k, l in SECTION_META],
                "ranks": [{"key": k, "label": l} for k, l in RANK_META],
                "cards": [{"key": k, "label": l} for k, l in CARD_META],
                "font_scales": list(FONT_SCALES),
                "widths": list(WIDTH_CHOICES),
                "card_cols": list(CARD_COLS_CHOICES),
            },
        }

    @app.get("/api/report-theme")
    async def get_report_theme():
        return _theme_payload()

    @app.put("/api/report-theme")
    async def put_report_theme(body: dict):
        try:
            th = mgr().theme_store.update(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"theme": th.public_dict()}

    @app.delete("/api/report-theme")
    async def reset_report_theme():
        th = mgr().theme_store.reset()
        return {"theme": th.public_dict()}

    @app.post("/api/report-theme/preview")
    async def preview_report_theme(body: dict):
        """用提交的主题（不落盘）渲染内置样例战报，供设置页即时预览。"""
        try:
            th = coerce_theme(body, mgr().theme_store.theme)
            png = await asyncio.to_thread(report_mod.render_demo_png, th)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    def _build_summary(rel: str, kind: str) -> dict:
        src = _safe_file(kind, rel)
        s = report_mod.summarize_file(src)
        sess = mgr().settings_store.settings.effective_sessdata()
        report_mod.enrich_online(s, sess)
        # 前端摘要不需要原始曲线点
        s["pop_points"] = len(s.pop("pop_series", []))
        s.pop("pop_series", None)
        s.pop("dm_bars", None)
        s.pop("top_texts", None)
        s.pop("top_sc", None)
        s.pop("top_medals", None)
        return s

    @app.post("/api/report/summary")
    async def report_summary(body: ReportIn):
        return await asyncio.to_thread(_build_summary, body.path, body.kind)

    @app.post("/api/report/image")
    async def report_image(body: ReportIn):
        def gen() -> bytes:
            src = _safe_file(body.kind, body.path)
            s = report_mod.summarize_file(src)
            report_mod.enrich_online(
                s, mgr().settings_store.settings.effective_sessdata())
            return report_mod.render_png(s, theme=mgr().theme_store.theme)
        png = await asyncio.to_thread(gen)
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/dedup/run-batch")
    async def dedup_run_batch(body: DedupBatchIn):
        results = []
        for rel in body.paths:
            results.append(await asyncio.to_thread(
                _do_dedup, rel, body.kind, body.window_sec, True))
        return {"results": results,
                "total_removed": sum(r["removed"] for r in results)}

    # ---------------- 视频录制 ----------------
    SESSION_BASE_RE = re.compile(r"^live_\d{8}_\d{4}$")

    def _vup_dir(vup: str) -> Path:
        base = mgr().settings.video_dir.resolve()
        p = (base / vup).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            raise HTTPException(status_code=403, detail="非法主播目录")
        return p

    def _load_session_meta(vup: str, session_base: str):
        if not SESSION_BASE_RE.match(session_base):
            raise HTTPException(status_code=400, detail="场次名格式非法")
        d = _vup_dir(vup)
        mf = d / f"{session_base}.meta.json"
        if not mf.is_file():
            raise HTTPException(status_code=404, detail="场次元数据不存在")
        meta = vr_mod.load_meta(mf)
        if not meta or meta.get("base") != session_base:
            raise HTTPException(status_code=500, detail="场次元数据缺失或损坏")
        return d, mf, meta

    def _meta_with_live(session: str, meta: dict) -> dict:
        """进行中场次：把正在写但尚未落入 meta 的当前分段补进副本。"""
        for rt in mgr().runtimes.values():
            if (rt.video_active and rt.video_session == session
                    and rt.video_file):
                seg_names = {s.get("file")
                             for s in meta.get("segments", [])}
                if rt.video_file not in seg_names:
                    m2 = dict(meta)
                    m2["segments"] = list(meta.get("segments", [])) + [{
                        "file": rt.video_file,
                        "start_ts": rt.video_started_at,
                        "end_ts": None,
                        "discarded": False,
                    }]
                    return m2
                break
        return meta

    def _enrich_ai_commands(vdir: Path, ai: dict) -> None:
        """给 C 档 AI 补漏窗口的 seek 注入播放器命令（rule 窗口走 highlights 接口）。"""
        ff = media_mod.find_ffmpeg()
        for w in ai.get("extra_windows", []):
            seek = w.get("seek")
            if seek and seek.get("part"):
                part_path = vdir / seek["part"]
                dur = max(1, seek["end_sec"] - seek["start_sec"])
                seek["commands"] = media_mod.player_commands(
                    ff, part_path, seek["start_sec"], dur)
        ai["ffmpeg_available"] = bool(ff)

    @app.get("/api/videos/ffmpeg")
    async def video_ffmpeg():
        ff = media_mod.find_ffmpeg()
        return {"available": bool(ff), **(ff or {})}

    @app.get("/api/videos")
    async def list_videos():
        m = mgr()
        root = m.settings.video_dir
        active = {rt.video_session: rt for rt in m.runtimes.values()
                  if rt.video_active and rt.video_session}
        targets = {t.room_id: t for t in m.target_store.targets}
        sessions: list[dict] = []
        orphans: list[dict] = []
        if root.exists():
            vdirs = (p for p in root.iterdir() if p.is_dir())
            for vdir in sorted(vdirs, key=lambda p: p.name.lower()):
                referenced: set[str] = set()
                for mf in sorted(vdir.glob("*.meta.json")):
                    meta = vr_mod.load_meta(mf)
                    if not meta:
                        continue
                    base = str(meta.get("base") or "")
                    segs, total = [], 0
                    for s in meta.get("segments", []):
                        fn = str(s.get("file") or "")
                        fp = vdir / fn if fn else None
                        exists = bool(fp and fp.is_file())
                        size = fp.stat().st_size if exists else 0
                        if fn:
                            referenced.add(fn.lower())
                        if exists and not s.get("discarded"):
                            total += size
                        segs.append({
                            "file": fn, "bytes": size, "exists": exists,
                            "discarded": bool(s.get("discarded")),
                            "start_ts": s.get("start_ts"),
                            "end_ts": s.get("end_ts"),
                            "result": s.get("result"),
                        })
                    clips = []
                    if base:
                        for cf in sorted(vdir.glob(f"{base}.clip_*.flv")):
                            referenced.add(cf.name.lower())
                            try:
                                clips.append({"file": cf.name,
                                              "size": cf.stat().st_size})
                            except OSError:
                                continue
                    # AI 解读缓存（<base>.ai.json）
                    ai_info = None
                    if base:
                        ap = vdir / f"{base}.ai.json"
                        if ap.is_file():
                            try:
                                aid = json.loads(
                                    ap.read_text(encoding="utf-8"))
                                if aid.get("_type") == "ai_highlights":
                                    ai_info = {
                                        "mode": aid.get("mode"),
                                        "ts": aid.get("ts"),
                                        "model": aid.get("model"),
                                        "summary": aid.get("summary"),
                                        "picks": len(aid.get("picks") or []),
                                    }
                            except (OSError, json.JSONDecodeError):
                                pass
                    ass_name = meta.get("ass")
                    ass_exists = bool(ass_name
                                      and (vdir / ass_name).is_file())
                    jsonl_name = meta.get("jsonl") or ""
                    ongoing = base in active
                    start_ts = float(meta.get("start_ts") or mf.stat().st_mtime)
                    end_ts = meta.get("end_ts")
                    # 异常退出（进程被杀/掉电）：meta 未收尾，认回磁盘残留分段
                    recovered = False
                    if base and not ongoing and not end_ts:
                        known = {s.get("file") for s in segs}
                        last_mtime = start_ts
                        for rf in sorted(vdir.glob(f"{base}*.flv")):
                            if ".clip_" in rf.name or rf.name in known:
                                continue
                            try:
                                rst_ = rf.stat()
                            except OSError:
                                continue
                            segs.append({
                                "file": rf.name, "bytes": rst_.st_size,
                                "exists": True, "discarded": False,
                                "start_ts": rst_.st_mtime,
                                "end_ts": rst_.st_mtime,
                                "result": "recovered",
                            })
                            total += rst_.st_size
                            referenced.add(rf.name.lower())
                            last_mtime = max(last_mtime, rst_.st_mtime)
                            recovered = True
                        if recovered:
                            end_ts = last_mtime
                    duration = int((end_ts or time.time()) - start_ts)
                    tgt = targets.get(meta.get("room_id"))
                    rt_live = active.get(base)
                    # 进行中的场次：meta 尚未落盘当前分段，用运行态补上实时大小
                    if rt_live and rt_live.video_file:
                        lp = vdir / rt_live.video_file
                        lsize = (lp.stat().st_size if lp.is_file()
                                 else int(rt_live.video_bytes or 0))
                        if not any(s.get("file") == rt_live.video_file
                                   for s in segs):
                            segs.append({
                                "file": rt_live.video_file,
                                "bytes": lsize, "exists": lp.is_file(),
                                "discarded": False,
                                "start_ts": rt_live.video_started_at,
                                "end_ts": None,
                                "result": "ongoing",
                            })
                            referenced.add(rt_live.video_file.lower())
                            if lp.is_file():
                                total += lsize
                    sessions.append({
                        "vup": vdir.name,
                        "base": base,
                        "room_id": meta.get("room_id"),
                        "title": meta.get("title", ""),
                        "anchor_name": meta.get("anchor_name", ""),
                        "start_ts": start_ts,
                        "start_clock": meta.get("start_clock", ""),
                        "end_ts": end_ts,
                        "end_reason": ("aborted" if recovered
                                        else meta.get("end_reason")),
                        "recovered": recovered,
                        "ongoing": ongoing,
                        "duration_sec": max(0, duration),
                        "bytes": total,
                        "qn": meta.get("qn"),
                        "qn_label": meta.get("qn_label", ""),
                        "codec": str(meta.get("codec", "")).upper(),
                        "container": meta.get("format", "flv"),
                        "segments": segs,
                        "segment_count": sum(1 for s in segs
                                             if s["exists"]
                                             and not s["discarded"]),
                        "ass": ass_name if ass_exists else None,
                        "jsonl": jsonl_name,
                        "jsonl_exists": bool(
                            jsonl_name
                            and (m.settings.records_dir / vdir.name
                                 / jsonl_name).is_file()),
                        "clips": clips,
                        "ai": ai_info,
                        "record_video": bool(
                            tgt.record_video if tgt else False),
                    })
                # 没有 meta 或已不属于任何场次的散片
                for f in sorted(vdir.glob("*.flv")):
                    if f.name.lower() in referenced:
                        continue
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    orphans.append({
                        "vup": vdir.name, "file": f.name,
                        "size": st.st_size, "mtime": st.st_mtime,
                    })
        sessions.sort(key=lambda x: x["start_ts"], reverse=True)
        orphans.sort(key=lambda x: x["mtime"], reverse=True)
        return {"dir": str(root), "ffmpeg": media_mod.find_ffmpeg(),
                "sessions": sessions, "orphans": orphans}

    @app.get("/api/videos/highlights")
    async def video_highlights(vup: str, session: str,
                               window_sec: int = Query(60, ge=20, le=600),
                               top_n: int = Query(8, ge=1, le=20)):
        d, _mf, meta = _load_session_meta(vup, session)
        jsonl = (mgr().settings.records_dir / vup
                 / str(meta.get("jsonl") or ""))
        # 进行中的场次：把正在写的分段补进 meta 副本，供高能窗口定位
        meta_for_detect = _meta_with_live(session, meta)
        try:
            result = await asyncio.to_thread(
                highlights_mod.detect_highlights,
                jsonl, meta_for_detect, window_sec, top_n)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        ff = media_mod.find_ffmpeg()
        for w in result.get("windows", []):
            seek = w.get("seek")
            if seek and seek.get("part"):
                part_path = d / seek["part"]
                dur = max(1, seek["end_sec"] - seek["start_sec"])
                seek["commands"] = media_mod.player_commands(
                    ff, part_path, seek["start_sec"], dur)
        result["ffmpeg"] = {"available": bool(ff), **(ff or {})}
        result["jsonl_exists"] = jsonl.exists()
        return result

    @app.get("/api/videos/ai-result")
    async def video_ai_result(vup: str, session: str):
        d, _mf, _meta = _load_session_meta(vup, session)
        ai = llm_mod.load_ai(d / f"{session}.ai.json")
        if not ai:
            raise HTTPException(status_code=404, detail="本场还没有 AI 解读结果")
        _enrich_ai_commands(d, ai)
        return ai

    @app.post("/api/videos/ai-analyze")
    async def video_ai_analyze(body: VideoAiIn):
        m = mgr()
        if not m.settings.llm_ready():
            raise HTTPException(
                status_code=400,
                detail="AI 接口未配置完整，请先在「设置 → AI 高能解读」填写")
        mode = (body.mode or m.settings.llm_mode or "A").upper()
        if mode not in LLM_MODES:
            raise HTTPException(status_code=400,
                                detail=f"AI 分析模式无效，可选：{LLM_MODES}")
        d, _mf, meta = _load_session_meta(body.vup, body.session)
        jsonl = (m.settings.records_dir / body.vup
                 / str(meta.get("jsonl") or ""))
        if not jsonl.is_file():
            raise HTTPException(status_code=400,
                                detail="本场弹幕数据文件不存在，无法解读")
        meta_for_detect = _meta_with_live(body.session, meta)
        try:
            rule_result = await asyncio.to_thread(
                highlights_mod.detect_highlights,
                jsonl, meta_for_detect, body.window_sec, body.top_n)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except (ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        ai_path = d / f"{body.session}.ai.json"
        try:
            ai = await llm_mod.analyze_session(
                settings=m.settings, jsonl_path=jsonl,
                meta=meta_for_detect, rule_result=rule_result,
                mode=mode, ai_path=ai_path, force=body.force)
        except llm_mod.LLMConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except llm_mod.LLMRuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))
        _enrich_ai_commands(d, ai)
        return ai

    @app.post("/api/videos/cut")
    async def video_cut(body: VideoCutIn):
        d, _mf, meta = _load_session_meta(body.vup, body.session)
        if body.end_sec <= body.start_sec:
            raise HTTPException(status_code=400,
                                detail="结束秒数必须大于起始秒数")
        seg_names = {str(s.get("file")) for s in meta.get("segments", [])
                     if not s.get("discarded")}
        # 进行中的当前分段尚未写入 meta，允许对其切片
        live_part = ""
        for rt0 in mgr().runtimes.values():
            if (rt0.video_active
                    and rt0.video_session == body.session):
                live_part = rt0.video_file or ""
                seg_names.add(live_part)
        if body.part not in seg_names:
            raise HTTPException(status_code=400,
                                detail="该分段不属于此场次或已被丢弃")
        src = (d / body.part).resolve()
        d_resolved = d.resolve()
        try:
            src.relative_to(d_resolved)
        except ValueError:
            raise HTTPException(status_code=403, detail="非法路径")
        if not src.is_file():
            raise HTTPException(status_code=404, detail="分段文件不存在")
        duration = min(body.end_sec - body.start_sec, 900)
        dst = d / f"{body.session}.clip_{int(body.start_sec)}.flv"
        try:
            r = await asyncio.to_thread(
                media_mod.cut_clip, src, dst, body.start_sec, duration)
        except (RuntimeError, FileNotFoundError, OSError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True, **r}

    @app.post("/api/videos/regenerate-ass")
    async def video_regen_ass(body: VideoSessionIn):
        d, mf, meta = _load_session_meta(body.vup, body.session)
        jsonl = (mgr().settings.records_dir / body.vup
                 / str(meta.get("jsonl") or ""))
        ass_path = d / f"{body.session}.ass"

        def _gen() -> str | None:
            name = vr_mod.generate_ass(jsonl, ass_path, meta)
            if name:
                meta["ass"] = name
                vr_mod.write_meta(meta, mf)
            return name

        try:
            name = await asyncio.to_thread(_gen)
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not name:
            raise HTTPException(
                status_code=400,
                detail="本场时间窗内没有可生成字幕的弹幕/SC 记录")
        return {"ok": True, "ass": name}

    @app.get("/api/videos/download")
    async def video_download(vup: str, file: str):
        d = _vup_dir(vup).resolve()
        p = (d / file).resolve()
        try:
            p.relative_to(d)
        except ValueError:
            raise HTTPException(status_code=403, detail="非法路径")
        if not p.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        if p.suffix.lower() not in (".flv", ".ass"):
            raise HTTPException(status_code=400,
                                detail="仅允许下载 FLV / ASS 文件")
        media_type = ("video/x-flv" if p.suffix.lower() == ".flv"
                      else "text/plain; charset=utf-8")
        return FileResponse(p, filename=p.name, media_type=media_type)

    @app.delete("/api/videos")
    async def delete_video(vup: str, session: str):
        d, _mf, meta = _load_session_meta(vup, session)
        if str(meta.get("base")) in {
                rt.video_session for rt in mgr().runtimes.values()
                if rt.video_active}:
            raise HTTPException(status_code=409,
                                detail="该场次正在录制中，不能删除")
        removed = []
        candidates = []
        for s in meta.get("segments", []):
            fn = s.get("file")
            if fn:
                candidates.append(d / fn)
        base = str(meta.get("base") or "")
        if base:
            candidates.append(d / f"{base}.ass")
            candidates.append(d / f"{base}.meta.json")
            candidates.append(d / f"{base}.meta.json.tmp")
            candidates.append(d / f"{base}.ai.json")
            candidates.append(d / f"{base}.ai.json.tmp")
            candidates.extend(d.glob(f"{base}.clip_*.flv"))
            # 异常认回的分段（meta 未记录）
            if not meta.get("end_ts"):
                for rf in d.glob(f"{base}*.flv"):
                    if ".clip_" not in rf.name:
                        candidates.append(rf)
        d_resolved = d.resolve()
        for p in candidates:
            try:
                p.resolve().relative_to(d_resolved)
            except ValueError:
                continue
            try:
                if p.is_file():
                    p.unlink()
                    removed.append(p.name)
            except OSError:
                continue
        return {"ok": True, "removed": removed}

    @app.delete("/api/videos/file")
    async def delete_video_file(vup: str, file: str):
        """删除散片（无 meta 归属的 FLV）；正在写入的分段受保护。"""
        d = _vup_dir(vup).resolve()
        p = (d / file).resolve()
        try:
            p.relative_to(d)
        except ValueError:
            raise HTTPException(status_code=403, detail="非法路径")
        if p.suffix.lower() != ".flv" or not p.is_file():
            raise HTTPException(status_code=404, detail="FLV 文件不存在")
        for rt in mgr().runtimes.values():
            if rt.video_active and rt.video_file == p.name:
                raise HTTPException(status_code=409,
                                    detail="该文件正在录制中，不能删除")
        try:
            p.unlink()
        except OSError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"ok": True}

    # ---------------- WebSocket ----------------
    @app.websocket("/api/ws")
    async def ws_push(ws: WebSocket):
        await ws.accept()
        q = mgr().bus.subscribe()
        try:
            await ws.send_json({"type": "snapshot", "ts": time.time(),
                                "data": mgr().snapshot()})
            missed = mgr().bus.recent_logs(100)
            for rec in missed:
                await ws.send_json({"type": "log", "ts": rec["ts"], "data": rec})
            while True:
                evt = await q.get()
                await ws.send_json(evt)
        except WebSocketDisconnect:
            pass
        except (RuntimeError, ConnectionError):
            pass
        finally:
            mgr().bus.unsubscribe(q)

    # ---------------- 静态 UI（最后注册，兜底） ----------------
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
