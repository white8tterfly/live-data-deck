#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动入口：python run.py → 启动本地 Web 服务并自动打开浏览器。

参数:
    --host HOST       监听地址（默认 127.0.0.1，仅本机访问）
    --port PORT       端口（默认 8787）
    --no-browser      不自动打开浏览器
"""
from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from server.app import create_app


def main() -> None:
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

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
