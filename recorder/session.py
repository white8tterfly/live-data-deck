"""单个直播间的 HTTP 会话：开播状态、danmu token、候选 WS 节点。

一个 LiveRoomSession 跟随一个房间录制任务的整个生命周期（跨多次 WS 重连复用），
-352 风控时通过 refresh_credentials() 重建风控指纹与 wbi key。
"""
from __future__ import annotations

import json
import urllib.parse

import httpx

from . import protocol as P


class LiveRoomSession:
    def __init__(self, room_id: int, sessdata: str = ""):
        self.room_id = room_id
        self.sessdata = sessdata.strip()
        self.client = httpx.Client(
            headers=P.HEADERS,
            cookies={"SESSDATA": self.sessdata} if self.sessdata else None,
            timeout=15, follow_redirects=True,
        )
        P.init_risk_cookies(self.client)
        self.wbi_key = P.get_wbi_key(self.client)
        nav = self.client.get(
            "https://api.bilibili.com/x/web-interface/nav").json().get("data", {})
        self.uid = nav.get("mid", 0)  # token 绑定登录身份；游客=0
        self.buvid3 = self.client.cookies.get("buvid3", "")
        # 直播元信息
        info = self.client.get(
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": room_id}).json().get("data", {})
        self.real_room_id = info.get("room_id", room_id)
        self.live_status = info.get("live_status", 0)
        self.title = info.get("title", "")
        self.anchor_name = info.get("uname", "")
        self.cover_url = info.get("keyframe", "") or info.get("user_cover", "")
        # live_time 为 "YYYY-MM-DD HH:MM:SS" 字符串，开播时间
        self.live_start_str = info.get("live_time", "") or ""

    def refresh_credentials(self) -> None:
        """-352 风控后重建风控指纹 + wbi key。"""
        try:
            P.init_risk_cookies(self.client)
            self.wbi_key = P.get_wbi_key(self.client)
            self.buvid3 = self.client.cookies.get("buvid3", "")
        except Exception as e:
            raise RuntimeError(f"刷新签名凭证失败: {e}") from e

    def get_ws_info(self) -> tuple[str, list[str]]:
        """wbi 签名 getDanmuInfo → (token, 全部候选 ws_url)。"""
        params = P.wbi_sign({"id": self.room_id, "type": 0}, self.wbi_key)
        r = self.client.get(
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
            params=params)
        body = r.json()
        if body.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo 失败: code={body.get('code')} "
                               f"msg={body.get('message', '')[:60]}")
        d = body["data"]
        urls = [f"wss://{h['host']}:{h['wss_port']}/sub"
                for h in d.get("host_list", [])]
        return d["token"], urls or [P.WS_URL_FALLBACK]

    def ws_cookie_header(self) -> str:
        """WS 握手 Cookie：部分边缘节点会秒踢不带 Cookie 的连接。"""
        parts = []
        if self.sessdata:
            parts.append(f"SESSDATA={urllib.parse.quote(self.sessdata, safe='')}")
        for name in ("buvid3", "b_3", "b_4"):
            v = self.client.cookies.get(name)
            if v:
                parts.append(f"{name}={v}")
        return "; ".join(parts)

    def auth_payload(self, token: str) -> bytes:
        return json.dumps({
            "uid": self.uid, "roomid": self.real_room_id, "protover": 2,
            "platform": "web", "type": 2, "buvid": self.buvid3, "key": token,
        }).encode()

    def refresh_info(self) -> None:
        """重新拉取开播状态/标题/主播名。失败时保留旧值。"""
        body = self.client.get(
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": self.room_id}).json().get("data", {})
        if body:
            self.real_room_id = body.get("room_id", self.real_room_id)
            self.live_status = body.get("live_status", self.live_status)
            self.title = body.get("title", self.title)
            self.anchor_name = body.get("uname", self.anchor_name)
            self.cover_url = (body.get("keyframe", "")
                              or body.get("user_cover", "") or self.cover_url)
            self.live_start_str = body.get("live_time", "") or self.live_start_str

    def get_popularity(self) -> dict:
        """HTTP 拉取当前加权人气与累计看过文案。

        2026-10 起 WS 心跳应答（op=3）对所有节点固定回 1，不再含真实人气，
        故人气改走 getInfoByRoom：data.popularity.popularity（与 room_info.online 一致）。
        返回 {popularity:int, watched:str(仅直播中有值), live_status:int}。
        -352 时自动重建签名凭证后重试一次。
        """
        def _do() -> dict:
            params = P.wbi_sign({"room_id": self.room_id}, self.wbi_key)
            body = self.client.get(
                "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
                params=params).json()
            if body.get("code") == -352:
                self.refresh_credentials()
                params = P.wbi_sign({"room_id": self.room_id}, self.wbi_key)
                body = self.client.get(
                    "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
                    params=params).json()
            if body.get("code") != 0:
                raise RuntimeError(
                    f"getInfoByRoom code={body.get('code')} {str(body.get('message',''))[:40]}")
            d = body.get("data", {})
            ri = d.get("room_info", {}) or {}
            pop = int((d.get("popularity") or {}).get("popularity")
                      or ri.get("online") or 0)
            live_status = int(ri.get("live_status") or 0)
            watched = ""
            if live_status == 1:
                ws = d.get("watched_show") or d.get("watched") or {}
                watched = ws.get("text_large") or ws.get("show") or ""
            return {"popularity": pop, "watched": watched,
                    "live_status": live_status}
        try:
            return _do()
        except Exception:
            # 重建凭证后再兜底一次
            self.refresh_credentials()
            return _do()

    def get_play_info(self, qn: int = 10000) -> dict:
        """getRoomPlayInfo v2 → 选流后的全部 CDN 拉流地址。

        2026-10 实测（匿名 code=0）：
        - 流数据在 data.playurl_info.playurl.stream[]（旧版 data.playurl 已废弃为空）
        - 层级 stream(protocol_name) → format[](format_name)
          → codec[](codec_name/current_qn/base_url/url_info[])
        - 完整 URL = url_info.host + codec.base_url + url_info.extra
        - 游客被限 qn=250 超清（accept=[10000,250]），有效 SESSDATA 才给原画
        选流优先级：http_stream/flv/avc > http_stream/flv/hevc > 其它（HLS/fmp4 兜底）。
        返回 {protocol,format,codec,current_qn,accept_qn,qn_desc,urls[]}。
        """
        params = {
            "room_id": self.room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": qn,
            "platform": "web",
            "ptype": "8",
        }
        body = self.client.get(
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
            params=params).json()
        if body.get("code") != 0:
            raise RuntimeError(
                f"getRoomPlayInfo 失败: code={body.get('code')} "
                f"msg={str(body.get('message', ''))[:60]}")
        playurl = (((body.get("data") or {}).get("playurl_info") or {})
                   .get("playurl") or {})
        streams = playurl.get("stream") or []
        if not streams:
            raise RuntimeError("getRoomPlayInfo 未返回可用流（可能未开播）")

        def _rank(proto: str, fmt: str, codec: str) -> tuple:
            p = {"http_stream": 0, "http_hls": 1}.get(proto, 2)
            f = {"flv": 0, "ts": 1, "fmp4": 2}.get(fmt, 3)
            c = {"avc": 0, "hevc": 1}.get(codec, 2)
            return (p, f, c)

        best = None
        for st in streams:
            proto = st.get("protocol_name", "")
            for fm in st.get("format", []):
                fmt = fm.get("format_name", "")
                for ck in fm.get("codec", []):
                    codec = ck.get("codec_name", "")
                    rank = _rank(proto, fmt, codec)
                    if best is None or rank < best[0]:
                        best = (rank, proto, fmt, codec, ck)
        if best is None:
            raise RuntimeError("流列表为空")
        _, proto, fmt, codec, ck = best
        urls = []
        for ui in ck.get("url_info", []):
            host = ui.get("host", "")
            extra = ui.get("extra", "")
            base = ck.get("base_url", "")
            if host and base:
                urls.append(host + base + extra)
        if not urls:
            raise RuntimeError("流缺少 url_info（无 CDN 地址）")
        return {
            "protocol": proto,
            "format": fmt,
            "codec": codec,
            "current_qn": int(ck.get("current_qn") or qn),
            "accept_qn": [int(x) for x in ck.get("accept_qn") or []],
            "qn_desc": playurl.get("g_qn_desc") or [],
            "urls": urls,
        }

    def close(self) -> None:
        self.client.close()


def verify_sessdata(sessdata: str) -> dict:
    """校验 SESSDATA 是否有效。返回 {ok, mid, uname, is_vip, message}。"""
    with httpx.Client(
        headers=P.HEADERS,
        cookies={"SESSDATA": sessdata.strip()} if sessdata.strip() else None,
        timeout=12, follow_redirects=True,
    ) as client:
        data = client.get(
            "https://api.bilibili.com/x/web-interface/nav").json()
    if data.get("code") != 0:
        return {"ok": False, "mid": 0, "uname": "",
                "message": data.get("message", "接口返回错误")}
    d = data.get("data", {})
    if not d.get("isLogin"):
        return {"ok": False, "mid": 0, "uname": "",
                "message": "Cookie 未登录或已过期"}
    return {"ok": True, "mid": d.get("mid", 0),
            "uname": d.get("uname", ""), "message": "登录有效"}
