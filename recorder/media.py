"""ffmpeg 外部命令检测与「秒切候选片段」（流拷贝，不转码）。

ffmpeg 不随程序内置：PATH 中检测到才启用秒切；检测不到时 UI 降级为
仅展示 VLC/ffplay 跳转命令与手动命令行。MP4 转封装/合并/烧弹幕属 B 档。
"""
from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path


@functools.lru_cache(maxsize=1)
def find_ffmpeg() -> dict | None:
    """检测 PATH 中的 ffmpeg，返回 {path, version}；未安装返回 None。进程内缓存。"""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        # Windows 下若 ffmpeg 为 .bat 之类需 shell=False；which 命中的均为可直接执行体
        p = subprocess.run([exe, "-version"], capture_output=True,
                           timeout=8, text=True, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {"path": exe, "version": ""}
    first = (p.stdout or p.stderr or "").splitlines()
    version = ""
    if first:
        m = re.search(r"ffmpeg version (\S+)", first[0])
        version = m.group(1) if m else first[0][:40]
    return {"path": exe, "version": version}


def cut_clip(src: Path, dst: Path, start_sec: float,
             duration_sec: float) -> dict:
    """-ss 前置 + -c copy 秒切（关键帧对齐，起止可能有 1-3s 偏差，候选素材足够）。"""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("未检测到 ffmpeg：请安装 ffmpeg 并加入 PATH 后重试")
    if not src.is_file():
        raise FileNotFoundError(f"分段文件不存在：{src.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ff["path"], "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, start_sec):.2f}",
        "-i", str(src),
        "-t", f"{max(1.0, duration_sec):.2f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        str(dst),
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180,
                           text=True, errors="replace")
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("ffmpeg 执行超时（180s）") from e
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("ffmpeg 失败：" + " / ".join(tail)[:200])
    size = dst.stat().st_size if dst.exists() else 0
    return {"file": dst.name, "size": size,
            "cmd": " ".join(_quote(c) for c in cmd)}


def player_commands(ffmpeg: dict | None, video_path: Path,
                    start_sec: float, duration_sec: float) -> dict:
    """生成可复制的本地播放/切片命令（浏览器无法直接拉起本机播放器）。"""
    p = str(video_path)
    return {
        "vlc": f'vlc --start-time={max(0, int(start_sec))} "{p}"',
        "ffplay": f'ffplay -ss {max(0, int(start_sec))} -t {int(duration_sec)} "{p}"',
        "ffmpeg_copy": (
            f'ffmpeg -ss {max(0, int(start_sec))} -i "{p}" '
            f'-t {int(duration_sec)} -c copy "clip_{int(start_sec)}.flv"'
        ) if ffmpeg else None,
        "tip": "VLC/ffplay 需已安装并加入 PATH；在本机终端（PowerShell）中粘贴执行。",
    }


def _quote(s: str) -> str:
    return f'"{s}"' if " " in s else s
