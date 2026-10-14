"""FastAPI 服务：REST API + WebSocket 实时推送 + 静态 Web UI 托管。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from recorder import analysis as analysis_mod
from recorder import dedup as dedup_mod
from recorder import report as report_mod
from recorder.config import WEB_DIR
from recorder.manager import RecorderManager
from recorder.session import verify_sessdata
from recorder.theme import (CARD_META, CARD_COLS_CHOICES, COLOR_META,
                            FONT_SCALES, RANK_META, SECTION_META,
                            ReportTheme, WIDTH_CHOICES, coerce_theme)
from recorder import following as following_mod
from server.schemas import (AnalysisIn, CaredUserIn, CookieTestIn, DedupBatchIn,
                            DedupIn, ReportIn, TargetIn, TargetPatch,
                            TargetsBulkIn)

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

    app = FastAPI(title="B站直播弹幕录制器", version="1.2.0", lifespan=lifespan)

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

    # ---------------- 房间管理 ----------------
    @app.get("/api/targets")
    async def list_targets():
        return {"targets": [
            {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled,
             "added_at": t.added_at}
            for t in mgr().target_store.targets]}

    @app.post("/api/targets", status_code=201)
    async def add_target(body: TargetIn):
        try:
            t = await mgr().add_target(
                body.room_id, body.vup.strip(), body.enabled)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled}

    @app.put("/api/targets/{room_id}")
    async def update_target(room_id: int, body: TargetPatch):
        try:
            t = await mgr().update_target(
                room_id, body.vup, body.enabled)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"room_id": t.room_id, "vup": t.vup, "enabled": t.enabled}

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
