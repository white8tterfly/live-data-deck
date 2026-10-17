"""ffmpeg 外部命令检测、秒切候选片段与烧录弹幕字幕导出。

ffmpeg 不随程序内置：PATH 中检测到才启用秒切/烧录；检测不到时 UI 降级为
仅展示 VLC/ffplay 跳转命令与手动命令行。烧录弹幕版 MP4 仅由用户显式触发。
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
                    start_sec: float, duration_sec: float,
                    ass_path: Path | None = None) -> dict:
    """生成可复制的本地播放/切片命令（浏览器无法直接拉起本机播放器）。

    ass_path 存在时显式附加字幕参数：VLC 用 --sub-file，ffplay 用 ass 滤镜
    （Windows 盘符冒号按 ffmpeg filtergraph 规则转义），粘贴执行即带弹幕。
    """
    p = str(video_path)
    vlc = f'vlc --start-time={max(0, int(start_sec))}'
    ffplay = f'ffplay -ss {max(0, int(start_sec))} -t {int(duration_sec)}'
    if ass_path:
        vlc += f' --sub-file="{ass_path}"'
        ffplay += f' -vf "ass=\'{_filter_path(ass_path)}\'"'
    vlc += f' "{p}"'
    ffplay += f' "{p}"'
    return {
        "vlc": vlc,
        "ffplay": ffplay,
        "ffmpeg_copy": (
            f'ffmpeg -ss {max(0, int(start_sec))} -i "{p}" '
            f'-t {int(duration_sec)} -c copy "clip_{int(start_sec)}.flv"'
        ) if ffmpeg else None,
        "tip": ("VLC/ffplay 需已安装并加入 PATH；在本机终端（PowerShell）中粘贴执行。"
                "本场已生成弹幕字幕，命令已附带字幕参数。"
                if ass_path else
                "VLC/ffplay 需已安装并加入 PATH；在本机终端（PowerShell）中粘贴执行。"),
    }


def _filter_path(p: Path) -> str:
    """ffmpeg filtergraph 滤镜参数里的路径转义：\\ → /、: → \\:、' → \\'。"""
    return (str(p).replace("\\", "/")
            .replace(":", "\\:").replace("'", "\\'"))


_ASS_TIME_RE = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})\.(\d{1,2})$")


def _ass_time_to_sec(s: str) -> float | None:
    """ASS 时间戳 H:MM:SS.cc → 秒；格式不符返回 None。"""
    m = _ASS_TIME_RE.match(s.strip())
    if not m:
        return None
    h, mi, se, cs = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + se + cs / 100


def _sec_to_ass_time(t: float) -> str:
    """秒 → ASS 时间戳 H:MM:SS.cc（负值按 0 处理，厘秒进位截到 99）。"""
    t = max(0.0, t)
    h = int(t // 3600)
    mi = int(t % 3600 // 60)
    se = int(t % 60)
    cs = int(round((t - int(t)) * 100))
    if cs >= 100:
        cs = 99  # 避免进位越界，1cs 误差可忽略
    return f"{h}:{mi:02d}:{se:02d}.{cs:02d}"


def shift_ass(src: Path, dst: Path, offset: float) -> None:
    """把场次级时间轴的 ASS 平移为分段级（新时间 = 原时间 - offset）。

    整行（Start/End 都早于 offset）的 Dialogue 丢弃；offset <= 0 直接复制。
    用于第 N 段烧录时与源 FLV 的时间轴对齐。
    """
    if offset <= 0:
        shutil.copyfile(src, dst)
        return

    def shift_field(field: str) -> float | None:
        t = _ass_time_to_sec(field)
        return None if t is None else t - offset

    out: list[str] = []
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.startswith("Dialogue:"):
                out.append(line)
                continue
            # Dialogue: Layer,Start,End,Style,...  （Start/End 为 H:MM:SS.cc）
            try:
                fields = line.rstrip("\n").split(",", 9)
                new_start = shift_field(fields[1])
                new_end = shift_field(fields[2])
            except (IndexError, ValueError):
                out.append(line)
                continue
            if new_start is None or new_end is None or new_end <= 0:
                # 时间解析失败原样保留；整行早于偏移的丢弃
                if new_start is None and new_end is None:
                    out.append(line)
                continue
            fields[1] = _sec_to_ass_time(max(0.0, new_start))
            fields[2] = _sec_to_ass_time(new_end)
            out.append(",".join(fields) + "\n")
    dst.write_text("".join(out), encoding="utf-8")


_DUR_RE = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")

# Video: h264 (High), yuv420p(...), 1920x1080 [SAR 1:1 DAR 16:9], ...
_DIMS_RE = re.compile(r"Video:.*?(\d{3,5})x(\d{3,5})(?:[\s,\[]|$)")


def probe_dims(ffmpeg: dict, path: Path) -> tuple[int, int] | None:
    """用 ffmpeg -i 的 stderr 解析视频分辨率（FLV 头部探测失败时的兜底）。

    ffmpeg -i 无输出文件必然报错退出，但 stderr 会打印流信息，解析 Video 行的 WxH；
    失败返回 None。
    """
    try:
        p = subprocess.run(
            [ffmpeg["path"], "-hide_banner", "-i", str(path)],
            capture_output=True, timeout=30, text=True,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = _DIMS_RE.search(p.stderr or "")
    if not m:
        return None
    try:
        w, h = int(m.group(1)), int(m.group(2))
    except ValueError:
        return None
    if 160 <= w <= 7680 and 160 <= h <= 4320:
        return w, h
    return None


def probe_duration(ffmpeg: dict, path: Path) -> float | None:
    """用 ffmpeg -i 的 stderr 解析媒体时长（本机无 ffprobe 时的替代方案）。

    ffmpeg -i 无输出文件必然报错退出，但 stderr 会打印输入信息，解析其中的
    Duration 行；失败返回 None。
    """
    try:
        p = subprocess.run(
            [ffmpeg["path"], "-hide_banner", "-i", str(path)],
            capture_output=True, timeout=30, text=True,
            encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = _DUR_RE.search(p.stderr or "")
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def burn_subtitle(ffmpeg: dict, src: Path, ass_path: Path, dst: Path,
                  duration_hint: float | None = None,
                  progress_cb=None) -> dict:
    """ffmpeg 把 ASS 弹幕字幕烧录进视频，输出 MP4（H.264 重编码、音频流拷贝）。

    -vf ass 滤镜路径按 filtergraph 规则转义；-progress pipe:1 流式输出进度，
    duration_hint 已知时回调 progress_cb(0~100)。失败抛 RuntimeError（带 stderr 尾部）。
    """
    vf = f"ass='{_filter_path(ass_path)}'"
    cmd = [
        ffmpeg["path"], "-y", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise RuntimeError(f"ffmpeg 启动失败：{e}") from e
    assert p.stdout is not None
    for line in p.stdout:
        line = line.strip()
        # 只认无歧义的 out_time=HH:MM:SS.ffffff（out_time_ms 字段名实为微秒的历史坑，不用）
        if not (progress_cb and duration_hint
                and line.startswith("out_time=")):
            continue
        val = line.partition("=")[2]
        neg = val.startswith("-")
        if neg:
            val = val[1:]
        parts = val.split(":")
        if len(parts) != 3:
            continue
        try:
            sec = (int(parts[0]) * 3600 + int(parts[1]) * 60
                   + float(parts[2]))
        except ValueError:
            continue
        if neg:
            sec = -sec
        pct = min(100, int(max(0.0, sec) / duration_hint * 100))
        progress_cb(max(0, pct))
    p.wait()
    if p.returncode != 0:
        tail = (p.stderr.read() or "").strip().splitlines()[-3:] if p.stderr else []
        raise RuntimeError("ffmpeg 烧录失败：" + " / ".join(tail)[:300])
    size = dst.stat().st_size if dst.exists() else 0
    if progress_cb:
        progress_cb(100)
    return {"file": dst.name, "size": size}


def _quote(s: str) -> str:
    """带空格的参数补上双引号，让回显的命令行可直接粘贴执行。"""
    return f'"{s}"' if " " in s else s
