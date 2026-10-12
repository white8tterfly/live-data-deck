"""命令行录制模式（无 Web 界面），兼容旧脚本用法。

示例:
    python -m recorder.cli                       # 按 targets.json 录制
    python -m recorder.cli --room 22637261       # 临时录制单房间
    python -m recorder.cli --duration 3600       # 录 1 小时后退出
    python -m recorder.cli --idle-stop 60        # 全部关播 60 分钟后自动停止
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt

from .config import Target
from .manager import RecorderManager


async def _print_logs(mgr: RecorderManager) -> None:
    q = mgr.bus.subscribe()
    try:
        while True:
            evt = await q.get()
            if evt.get("type") == "log":
                d = evt["data"]
                ts = _dt.datetime.fromtimestamp(d["ts"]).strftime("%H:%M:%S")
                print(f"{ts} {d['msg']}")
    except asyncio.CancelledError:
        raise


async def _amain(args) -> None:
    mgr = RecorderManager()

    if args.keep_gifts:
        mgr.settings.record_gifts = True
    if args.idle_stop:
        mgr.settings.idle_stop_minutes = args.idle_stop
    if args.room:
        # 临时房间：只进内存，不写 targets.json
        mgr.target_store.targets = [
            Target(room_id=args.room, vup=f"room_{args.room}", enabled=True)]
        mgr.settings.auto_start = True

    await mgr.start()
    printer = asyncio.create_task(_print_logs(mgr))

    stop_task = None
    if args.duration:
        async def _timed_stop() -> None:
            await asyncio.sleep(args.duration)
            mgr.bus.log("info", f"已录制 {args.duration}s，到时停止")
            await mgr.stop_all()
        stop_task = asyncio.create_task(_timed_stop())

    try:
        # 等待所有录制任务结束（idle watchdog 停止 / duration 到时 / Ctrl+C）
        while True:
            await asyncio.sleep(2)
            if not any(mgr.is_running(t.room_id)
                       for t in mgr.target_store.targets):
                # auto_start 的错峰启动可能还没跑完，给一点宽限
                await asyncio.sleep(3)
                if not any(mgr.is_running(t.room_id)
                           for t in mgr.target_store.targets):
                    break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if stop_task:
            stop_task.cancel()
        printer.cancel()
        await mgr.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="B站直播弹幕录制器（命令行模式）")
    parser.add_argument("--room", type=int, help="临时录制单个房间号（不读 targets）")
    parser.add_argument("--duration", type=int, default=0,
                        help="录制秒数（0=不限，Ctrl+C 停止）")
    parser.add_argument("--idle-stop", type=int, default=0, dest="idle_stop",
                        help="全部房间关播 N 分钟后自动停止（0=不自动停止）")
    parser.add_argument("--keep-gifts", action="store_true", dest="keep_gifts",
                        help="保留礼物事件落盘（默认不录）")
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
