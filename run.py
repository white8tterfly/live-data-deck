#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动入口：python run.py 起本地 Web 服务，稍后自动打开浏览器。

--host 监听地址（默认 127.0.0.1，仅本机可达）、--port 端口（默认 8787）、
--no-browser 不自动开浏览器。应用实例来自 server.app.create_app。
绿色版（PyInstaller 冻结运行）在服务起来之后会把控制台窗口藏起来，并在通知区域挂一个图标：
窗口没了，Ctrl+C 与「关掉黑窗口」都不成立，用户得靠网页侧栏的「退出服务」或托盘的「退出」停机。
停机一律走 uvicorn 的 should_exit，不做硬杀。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time
import webbrowser

import uvicorn
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from server.app import create_app


def _process_image_path(pid: int) -> str:
    """取某个进程的可执行文件完整路径；拿不到（权限不足等）返回空串。"""
    k32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_uint32(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(handle)


def _console_is_ours() -> bool:
    """判断当前控制台是不是为本 exe 单独开的（双击启动的场景）。

    GetConsoleProcessList 列出挂在同一个控制台上的进程，逐个比可执行文件路径：
    全部都是本 exe 自己（PyInstaller 的引导进程与应用进程）才算「我们的窗口」；
    只要夹着别的镜像——从 cmd / PowerShell 启动时的父 shell——就不能动，
    否则会把用户自己的终端窗口一并藏掉。拿不到路径时按「不是我们的」处理。
    """
    k32 = ctypes.windll.kernel32
    arr = (ctypes.c_uint32 * 16)()
    count = k32.GetConsoleProcessList(arr, 16)
    if count <= 0:
        return False
    mine = k32.GetCurrentProcessId()
    if count == 1:
        return arr[0] == mine
    exe = os.path.normcase(os.path.abspath(sys.executable))
    for i in range(min(count, 16)):
        if arr[i] == mine:
            continue
        path = _process_image_path(arr[i])
        if not path or os.path.normcase(os.path.abspath(path)) != exe:
            return False
    return True


def _hide_console_window() -> None:
    """藏掉绿色版双击启动时那个黑色控制台窗口。

    只在 Windows + 冻结运行 + 控制台专属本 exe 时才动手；隐藏失败（无控制台、
    非 Windows、权限不足）静默跳过，不能因此影响服务启动。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd and _console_is_ours():
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # 0 = SW_HIDE
    except Exception:
        pass


def _hide_console_when_started(server: uvicorn.Server) -> None:
    """等 uvicorn 真正开始服务之后再隐藏控制台。

    启动阶段先不藏：端口被占用之类的失败信息要留在窗口里给用户看到，
    所以先等 started 标志，一直没起来（超时）就什么都不做。
    """
    for _ in range(200):                                # 最多等 20 秒
        if server.started:
            break
        time.sleep(0.1)
    else:
        return
    time.sleep(1.0)                                     # 先让启动提示与浏览器打开走完
    _hide_console_window()


# 通知区域图标（仅绿色版）
# ctypes 的 Windows 专用类型只在该系统上存在，收在条件里定义，保证本文件在 macOS / Linux 下照旧可导入
if os.name == "nt":
    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p,
                                  ctypes.c_uint, ctypes.c_size_t,
                                  ctypes.c_ssize_t)

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _MSG(ctypes.Structure):
        """GetMessageW 倒出来的消息结构，字段顺序与布局必须和 Win32 对齐。"""
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", ctypes.c_ulong), ("pt", _POINT)]

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", _WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                    ("hCursor", ctypes.c_void_p),
                    ("hbrBackground", ctypes.c_void_p),
                    ("lpszMenuName", ctypes.c_wchar_p),
                    ("lpszClassName", ctypes.c_wchar_p)]

    class _NOTIFYICONDATAW(ctypes.Structure):
        """托盘图标描述结构；用 Vista 之前的字段集，cbSize 传本结构自身大小即可。"""
        _fields_ = [("cbSize", ctypes.c_ulong), ("hWnd", ctypes.c_void_p),
                    ("uID", ctypes.c_uint), ("uFlags", ctypes.c_uint),
                    ("uCallbackMessage", ctypes.c_uint),
                    ("hIcon", ctypes.c_void_p),
                    ("szTip", ctypes.c_wchar * 128),
                    ("dwState", ctypes.c_ulong),
                    ("dwStateMask", ctypes.c_ulong),
                    ("szInfo", ctypes.c_wchar * 256),
                    ("uTimeoutOrVersion", ctypes.c_uint),
                    ("szInfoTitle", ctypes.c_wchar * 64),
                    ("dwInfoFlags", ctypes.c_ulong)]


_TRAY_CLASS_NAME = "LiveDataDeckTray"
_TRAY_CALLBACK_MSG = 0x8000 + 1         # WM_APP + 1，托盘回调发给隐藏窗口的消息
_TRAY_UID = 1
_TRAY_MENU_OPEN = 1
_TRAY_MENU_QUIT = 2

# 托盘线程与主线程共享的状态：句柄、图标描述、菜单动作与窗口过程（过程引用要一直留着，否则会被回收）
_tray = {"hwnd": None, "nid": None, "added": False,
         "open_url": "", "on_quit": None, "wndproc": None}


def _remove_tray_icon() -> None:
    """摘掉通知区域图标；没建过或已经摘过就什么都不做。"""
    nid = _tray["nid"]
    if nid is None or not _tray["added"]:
        return
    try:
        ctypes.windll.shell32.Shell_NotifyIconW(2, ctypes.byref(nid))   # NIM_DELETE
    except Exception:
        pass
    _tray["added"] = False


def _show_tray_menu(hwnd) -> None:
    """弹出托盘右键菜单。

    用 TPM_RETURNCMD 直接把选中项取回来，省掉把 WM_COMMAND 转发一遍的麻烦；
    弹之前先抢前台窗口，否则点菜单外面菜单不会自动收起（Windows 的老规矩）。
    """
    user32 = ctypes.windll.user32
    menu = ctypes.c_void_p(user32.CreatePopupMenu())
    if not menu:
        return
    user32.AppendMenuW(menu, 0, _TRAY_MENU_OPEN, "打开界面")
    user32.AppendMenuW(menu, 0, _TRAY_MENU_QUIT, "退出")
    pt = _POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(menu, 0x0002 | 0x0100,      # RIGHTBUTTON | RETURNCMD
                                pt.x, pt.y, 0, hwnd, None)
    user32.PostMessageW(hwnd, 0, 0, 0)
    user32.DestroyMenu(menu)
    if cmd == _TRAY_MENU_OPEN and _tray["open_url"]:
        webbrowser.open(_tray["open_url"])
    elif cmd == _TRAY_MENU_QUIT and _tray["on_quit"]:
        _tray["on_quit"]()


def _tray_wnd_proc(hwnd, msg, wparam, lparam):
    """隐藏窗口的窗口过程：只认托盘回调消息与窗口销毁，其余交回系统默认处理。"""
    if msg == _TRAY_CALLBACK_MSG:
        if lparam in (0x0202, 0x0205):                      # 左键 / 右键弹起
            _show_tray_menu(hwnd)
    elif msg == 0x0002:                                     # WM_DESTROY
        ctypes.windll.user32.PostQuitMessage(0)
    return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _tray_loop(open_url: str, on_quit) -> None:
    """托盘线程主体：注册窗口类、建隐藏窗口、挂图标，然后跑消息循环。

    全程包在 try 里：托盘建不起来（系统限制、资源不足）顶多是没图标，
    不能把服务启动带崩。
    """
    try:
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        # 句柄用 c_void_p 承接：默认返回类型是 c_int，会把 64 位句柄截断，再传回去就错了
        user32.LoadIconW.restype = ctypes.c_void_p
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreatePopupMenu.restype = ctypes.c_void_p
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        _tray["open_url"] = open_url
        _tray["on_quit"] = on_quit
        _tray["wndproc"] = _WNDPROC(_tray_wnd_proc)

        hinst = ctypes.c_void_p(kernel32.GetModuleHandleW(None))
        wc = _WNDCLASSW()
        wc.lpfnWndProc = _tray["wndproc"]
        wc.hInstance = hinst
        wc.lpszClassName = _TRAY_CLASS_NAME
        user32.RegisterClassW(ctypes.byref(wc))
        hwnd = ctypes.c_void_p(user32.CreateWindowExW(
            0, _TRAY_CLASS_NAME, "LiveDataDeck", 0,
            0, 0, 0, 0, None, None, hinst, None))
        if not hwnd:
            return
        _tray["hwnd"] = hwnd

        hicon = ctypes.c_void_p(user32.LoadIconW(hinst, ctypes.c_void_p(1)))
        if not hicon:                                       # 取不到 exe 自带图标就退回系统默认图标
            hicon = ctypes.c_void_p(user32.LoadIconW(None, ctypes.c_void_p(32512)))

        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = _TRAY_UID
        nid.uFlags = 0x1 | 0x2 | 0x4                        # MESSAGE | ICON | TIP
        nid.uCallbackMessage = _TRAY_CALLBACK_MSG
        nid.hIcon = hicon
        nid.szTip = "直播数据舱 · 录制中"
        if not shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):         # NIM_ADD
            return
        _tray["nid"] = nid
        _tray["added"] = True

        msg = _MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except Exception:
        return


def _start_tray(open_url: str, on_quit) -> None:
    """按需拉起托盘线程：只认 Windows + 冻结运行，与「控制台自动隐藏」同一判据。

    源码运行控制台本来就在，Ctrl+C 足够，没必要再占一个通知区域的位置。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    threading.Thread(target=_tray_loop, args=(open_url, on_quit),
                     daemon=True).start()


def _register_shutdown_route(app, endpoint) -> None:
    """把停机接口挂到静态资源兜底路由之前。

    create_app 末尾用 app.mount("/", StaticFiles(...)) 兜底，Starlette 按注册顺序逐条匹配，
    直接 add_api_route 会排在兜底之后、永远轮不到，所以取那个 Mount 的下标插到它前面。
    """
    routes = app.router.routes
    index = next((i for i, r in enumerate(routes) if isinstance(r, Mount)),
                 len(routes))
    routes.insert(index, Route("/api/shutdown", endpoint, methods=["POST"]))


def main() -> None:
    """解析命令行参数，拉起 uvicorn，按需延时自动开浏览器。"""
    parser = argparse.ArgumentParser(description="B站直播弹幕录制器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true", dest="no_browser")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        print(f"录制器启动中，浏览器将自动打开：{url}")
    else:
        print(f"录制器启动：{url}")

    app = create_app()
    server = uvicorn.Server(uvicorn.Config(
        app, host=args.host, port=args.port, log_level="warning"))

    def request_shutdown() -> None:
        """优雅停机：先摘托盘图标，再延迟置 uvicorn 的 should_exit。

        延迟是为了让当前这次 HTTP 响应先发回浏览器；置位后 uvicorn 停掉监听、跑完
        lifespan 的关闭分支（mgr.shutdown 收尾各房间录制）。绝不能换成 os._exit 之类硬杀。
        """
        _remove_tray_icon()
        threading.Timer(0.4, lambda: setattr(server, "should_exit", True)).start()

    async def shutdown_endpoint(_request):
        """网页侧栏「退出服务」按钮打过来的停机请求。"""
        request_shutdown()
        return JSONResponse({"ok": True})

    _register_shutdown_route(app, shutdown_endpoint)

    if getattr(sys, "frozen", False):
        threading.Thread(target=_hide_console_when_started,
                         args=(server,), daemon=True).start()
    _start_tray(url, request_shutdown)
    server.run()


if __name__ == "__main__":
    main()
