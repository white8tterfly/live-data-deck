"""B 站网页版扫码登录：半自动获取 SESSDATA。

整个流程由本机后端代理（前端不直连 passport.bilibili.com，规避浏览器跨域）：
1. generate_qr()：调 qrcode/generate 拿 qrcode_key + 扫码 URL，
   URL 在后端编码成二维码 PNG（base64），前端直接展示。
2. poll_qr()：轮询 qrcode/poll，返回 waiting / scanned / success / expired / error；
   登录成功后取回 SESSDATA 返回给前端填入输入框——老版本 B 站把它拼在跳转 URL 的
   查询串里，现版本改成只下发一个带 ticket 的 crossDomain 地址，凭证由访问该地址时
   的 Set-Cookie 提供，两种形态都要兼容；
   确认了登录却仍取不到凭证时返回 error（而不是抛错），避免调用方继续轮询一个
   已被消费的二维码、把「已确认」误报成「已过期」。

后端不持久化凭证：拿到 SESSDATA 后由前端填入，用户点「保存设置」才落盘。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
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


_session: httpx.Client | None = None


def _client() -> httpx.Client:
    """扫码登录全程共用的 HTTP 会话（首次调用创建，被关闭后重建）。

    这里必须共用而不是「一次请求一个客户端」：B 站会在 generate 二维码时下发
    风控 cookie（buvid3 / b_nut 等），换成新会话再 poll 会被当成另一个客户端；
    顺带复用连接池，省掉 1.8 秒一轮的反复建连。
    """
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.Client(
            timeout=12,
            headers={"User-Agent": _UA,
                     "Referer": "https://passport.bilibili.com/"})
    return _session


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
    """查询扫码状态。

    返回 {status, ...}，status ∈ waiting / scanned / success / expired / error。
    成功时带 sessdata；error 表示 B 站已确认登录但没取到凭证。
    """
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
        sess = _obtain_sessdata(data)
        if not sess:
            # 已经确认登录却拿不到凭证时不能抛错让前端接着轮询：二维码此时已被消费，
            # 下一轮 poll 只会拿到 86038，用户就会看到「确认登录后反而提示二维码已过期」。
            _log_unparsed_success(data)
            return {"status": "error",
                    "message": "已确认登录但未取到 SESSDATA，请刷新二维码重试"}
        return {"status": "success", "sessdata": sess,
                "message": "扫码登录成功"}
    return {
        "status": _CODE_STATUS.get(code, "waiting"),
        "code": code,
        "message": data.get("message") or j.get("message") or "",
    }


def _log_unparsed_success(data: dict) -> None:
    """成功响应里找不到 SESSDATA 时留一条脱敏诊断，用于判断 B 站返回格式是否变了。

    响应里除 SESSDATA 外还可能带 ticket、refresh_token 这类一次性凭证，
    一并打码后再落日志，避免明文凭证留在日志文件里。
    """
    raw = json.dumps(data, ensure_ascii=False)
    for pattern, repl in (
            (r"(SESSDATA=)[^&\"']*", r"\1***"),
            (r"(\"refresh_token\":\s*\")[^\"]*", r"\1***"),
            (r"(ticket=)[^&\"']*", r"\1***")):
        raw = re.sub(pattern, repl, raw)
    logging.getLogger("ldd.bili_login").warning(
        "扫码登录已确认但未取到 SESSDATA，响应片段：%s", raw[:300])


def _obtain_sessdata(data: dict) -> str:
    """从登录成功响应里取回 SESSDATA，取不到返回空串。

    兼容两种响应形态：
    - 老形态：data.url 的查询串里直接带 SESSDATA；
    - 新形态（2026-10 实测）：data.url 只给一个带 ticket 的 crossDomain 地址，
      SESSDATA 由访问该地址时下发的 Set-Cookie 提供。
    新形态必须带着本次会话去走那一跳，cookie 才会落到会话罐里，所以不能只解析地址。

    返回的是**解码后**的值：前端粘贴提取走 decodeURIComponent、session.py 发送前
    再用 quote 编码，两端都按「存解码值」的约定，这里必须对齐。
    """
    jump = data.get("url") or data.get("redirect_url") or ""
    sess = _extract_sessdata(jump)              # 老形态：地址里直接带
    if sess:
        return sess
    client = _client()
    sess = _sessdata_from_jar(client)           # poll 响应自身就下发的情形
    if sess:
        return sess
    if not jump:
        return ""
    try:
        _get_without_referer(client, jump)
    except httpx.HTTPError as e:
        # 跟随重定向失败也先别放弃：Set-Cookie 在跳转之前就已经落进罐里了
        logging.getLogger("ldd.bili_login").warning(
            "访问 crossDomain 换取凭证失败：%s", e)
    return _sessdata_from_jar(client)


def _sessdata_from_jar(client: httpx.Client) -> str:
    """在会话 cookie 罐里找 SESSDATA，找到返回解码后的值，找不到返回空串。"""
    for cookie in client.cookies.jar:
        if cookie.name == "SESSDATA" and cookie.value:
            return urllib.parse.unquote(cookie.value)
    return ""


def _get_without_referer(client: httpx.Client, url: str) -> None:
    """带会话访问 crossDomain 地址，但摘掉会话上默认的 Referer。

    会话默认 Referer 是 passport.bilibili.com，而这一跳落到 passport.biligame.com；
    跨站还带着不对口的来源头容易被判成异常请求。
    """
    req = client.build_request("GET", url)
    req.headers.pop("referer", None)
    client.send(req, follow_redirects=True)


def _extract_sessdata(jump_url: str) -> str:
    """从扫码成功后的跨域跳转 URL 里取 SESSDATA。

    B 站偶尔会把整条 url 再整体百分号编码一层（此时按原文解析拿不到查询串），
    所以原样试一次、解一层再试一次。
    """
    if not jump_url:
        return ""
    for text in (jump_url, urllib.parse.unquote(jump_url)):
        try:
            query = urllib.parse.urlparse(text).query
        except ValueError:
            continue
        vals = urllib.parse.parse_qs(query).get("SESSDATA") or []
        if vals:
            return vals[0]
    return ""
