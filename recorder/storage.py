"""jsonl 落盘：弹幕记录（records/）与房管处置弱标签（weak_label/）。

懒落盘原则：仅 live_status==1（真实开播）才建文件；
未开播(0)/轮播(2) 不创建任何文件，内容直接丢弃。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import RoomRuntime
from .session import LiveRoomSession


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def ensure_out_file(session: LiveRoomSession, rt: RoomRuntime,
                    records_dir: Path) -> Path | None:
    """确认开播才创建目录 + 当天 jsonl + session_header（已存在则复用）。"""
    if rt.current_file:
        return records_dir / rt.vup / rt.current_file
    if session.live_status != 1:
        return None
    try:
        session.refresh_info()
    except Exception:
        pass
    if session.live_status != 1:
        return None
    out_dir = records_dir / rt.vup
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"live_{_today()}.jsonl"
    out_file = out_dir / name
    if (not out_file.exists()) or out_file.stat().st_size == 0:
        header = {
            "_type": "session_header", "room_id": session.real_room_id,
            "vup": rt.vup, "anchor_name": session.anchor_name,
            "title": session.title, "live_status": session.live_status,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        with out_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
    rt.current_file = name
    return out_file


def append_record(out_file: Path, rec: dict, rt: RoomRuntime, kind: str) -> None:
    rt.bump(kind)
    with out_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_moderation(out_dir: Path, vup: str, rec: dict) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"weak_{_today()}.jsonl"
    with out_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return out_file
