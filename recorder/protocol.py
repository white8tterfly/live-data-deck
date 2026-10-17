"""B 站直播弹幕 WebSocket 协议：封包、浏览器指纹、wbi 签名。

协议要点（2026-09 实测）：
1. getDanmuInfo 需 wbi 签名（否则 -352 风控）→ token + host_list
2. auth 的 uid 必须与 SESSDATA 身份匹配（token 绑定登录者）
3. 连 host_list 边缘节点，应用层心跳 ~25s
4. protover=2 消息体为 zlib 压缩（可内嵌多个子包）
5. websockets 连接需 ping_interval=None；握手带浏览器同款 UA/Origin/Cookie
"""
from __future__ import annotations

import hashlib
import struct
import time
import urllib.parse

import httpx

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

WS_URL_FALLBACK = "wss://broadcastlv.chat.bilibili.com/sub"
HEARTBEAT_INTERVAL = 25
OFFLINE_POLL_SEC = 45

# wbi 签名混淆表与下方签名流程：为与 Web 端正常互操作，对浏览器公开可观察的
# 请求行为所做的独立实现，不含平台专有源码（详见根目录 NOTICE 第四节）。
MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
             43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48,
             7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21,
             56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]


def pack(op: int, payload: bytes = b"") -> bytes:
    """组装客户端协议包（大端：包长/头长/协议版本/操作码/序列）。"""
    return struct.pack(">IHHII", 16 + len(payload), 16, 1, op, 1) + payload


def init_risk_cookies(client: httpx.Client) -> None:
    """预热风控指纹：buvid 系 cookie（b_3/b_4）。"""
    client.get("https://www.bilibili.com/")
    try:
        d = client.get(
            "https://api.bilibili.com/x/frontend/finger/spi").json()["data"]
        for k in ("b_3", "b_4"):
            client.cookies.set(k, d[k], domain=".bilibili.com")
    except Exception:
        pass


def get_wbi_key(client: httpx.Client) -> str:
    """从导航接口取 img/sub 密钥并按混淆表生成 32 位 mixin key。"""
    nav = client.get("https://api.bilibili.com/x/web-interface/nav").json()["data"]
    wbi = nav["wbi_img"]
    raw = (wbi["img_url"].rsplit("/", 1)[-1].split(".")[0]
           + wbi["sub_url"].rsplit("/", 1)[-1].split(".")[0])
    return "".join(raw[i] for i in MIXIN_TAB if i < len(raw))[:32]


def wbi_sign(params: dict, mixin_key: str) -> dict:
    """按 B 站 wbi 规则补 wts、过滤特殊字符并算出 w_rid，返回新 dict（不改入参）。"""
    params = dict(params)
    params["wts"] = int(time.time())
    params = {k: "".join(c for c in str(v) if c not in "!'()*")
              for k, v in params.items()}
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()
    return params
