"""FastAPI 服务：REST API + WebSocket 实时推送 + 静态 Web UI 托管。"""
from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from recorder.config import WEB_DIR
from recorder.manager import RecorderManager
from recorder.session import verify_sessdata
from server.schemas import CookieTestIn, TargetIn, TargetPatch

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

    app = FastAPI(title="B站直播弹幕录制器", version="1.0.0", lifespan=lifespan)

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
