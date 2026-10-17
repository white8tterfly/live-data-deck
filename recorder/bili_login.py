"""B 站网页版扫码登录：半自动获取 SESSDATA。

整个流程由本机后端代理（前端不直连 passport.bilibili.com，规避浏览器跨域）：
1. generate_qr()：调 qrcode/generate 拿 qrcode_key + 扫码 URL，
   URL 在后端编码成二维码 PNG（base64），前端直接展示。
2. poll_qr()：轮询 qrcode/poll，返回 waiting / scanned / success / expired；
   登录成功后从跳转 URL 的查询参数解析 SESSDATA 返回给前端填入输入框。

后端不持久化凭证：拿到 SESSDATA 后由前端填入，用户点「保存设置」才落盘。
"""
from __future__ import annotations

import base64
import io
import urllib.parse

import httpx
import qrcode

QR_GENERATE = (
    "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# B 站 poll 接口业务码（在 data.code 里；外层 code=0 只代表请求成功）：
# 0=登录成功；86090=已扫码待手机确认；86101/86091=未扫码等待；86038=二维码过期
_CODE_STATUS = {0: "success", 86090: "scanned", 86101: "waiting",
                86091: "waiting", 86038: "expired"}


class BiliLoginError(RuntimeError):
    """扫码登录流程出错（请求失败 / B 站返回异常 / 解析失败）。"""


def _client() -> httpx.Client:
    """单次请求用的短超时客户端（登录接口无需保持长连接）。"""
    return httpx.Client(
        timeout=12,
        headers={"User-Agent": _UA,
                 "Referer": "https://passport.bilibili.com/"})


def generate_qr() -> dict:
    """向 B 站申请登录二维码，返回 {qrcode_key, qr_image(data URL)}。"""
    try:
        r = _client().get(QR_GENERATE)
        j = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BiliLoginError(f"请求 B 站二维码接口失败：{e}")
    if j.get("code") != 0 or not isinstance(j.get("data"), dict):
        raise BiliLoginError(
            f"B 站返回异常：{j.get('message') or '无法生成二维码'}")
    url = j["data"].get("url") or ""
    key = j["data"].get("qrcode_key") or ""
    if not url or not key:
        raise BiliLoginError("二维码接口缺少 url / qrcode_key")
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"qrcode_key": key, "qr_image": f"data:image/png;base64,{b64}"}


def poll_qr(qrcode_key: str) -> dict:
    """查询扫码状态。成功时返回 {status:'success', sessdata, message}。"""
    key = (qrcode_key or "").strip()
    if not key:
        raise BiliLoginError("缺少 qrcode_key")
    try:
        r = _client().get(QR_POLL, params={"qrcode_key": key})
        j = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise BiliLoginError(f"轮询二维码状态失败：{e}")
    outer = j.get("code")
    if outer != 0 or not isinstance(j.get("data"), dict):
        raise BiliLoginError(
            f"B 站返回异常：{j.get('message') or '无法查询扫码状态'}")
    data = j["data"]
    code = data.get("code")
    if code == 0:
        jump = data.get("url") or data.get("redirect_url") or ""
        sess = _extract_sessdata(jump)
        if not sess:
            raise BiliLoginError("登录成功但未能解析 SESSDATA")
        return {"status": "success", "sessdata": sess,
                "message": "扫码登录成功"}
    return {
        "status": _CODE_STATUS.get(code, "waiting"),
        "code": code,
        "message": data.get("message") or j.get("message") or "",
    }


def _extract_sessdata(jump_url: str) -> str:
    """扫码成功后 B 站给的是带 SESSDATA 查询参数的跨域跳转 URL。"""
    if not jump_url:
        return ""
    try:
        query = urllib.parse.urlparse(jump_url).query
        vals = urllib.parse.parse_qs(query).get("SESSDATA") or []
        return vals[0] if vals else ""
    except ValueError:
        return ""
