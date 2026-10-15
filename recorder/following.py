"""B 站直播关注列表：读取「我关注的主播」及其开播状态（需要登录态）。

接口 /xlive/web-ucenter/user/following 是直播中心「我的关注」数据源，
返回当前账号关注的直播间（开播的排最前），游客调用返回 -101。
与弹幕通道不同：弹幕 WS 游客可连，关注列表属于私有数据，必须带有效 SESSDATA。

分页实测坑（2026-10-15 抓包确认）：
- page_size 只接受小值，20 合法；30/50 会被服务端**静默拒绝**——降级为 10 条/页，
  且连 page 参数一并忽略，任意页码都返回相同的第 1 页 10 条（totalPage 等字段照常，
  极具迷惑性）。因此 PAGE_SIZE 固定 20，禁止调大。
- 缺少风控 cookie（buvid3 等）时即使 page_size=10 也会触发同样的静默降级，
  请求前必须调用 P.init_risk_cookies()。
"""
from __future__ import annotations

import httpx

from . import protocol as P

FOLLOWING_URL = ("https://api.live.bilibili.com/xlive/web-ucenter/"
                 "user/following")
PAGE_SIZE = 20          # 实测合法值；30/50 会被静默降级，禁止调大
MAX_PAGE = 1000         # 参数上限保护，真实总页数由 totalPage 给出


class FollowingError(Exception):
    """关注列表拉取失败。code 为 B 站业务码（如 -101 未登录）。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _norm_item(raw: dict) -> dict | None:
    room_id = raw.get("roomid") or raw.get("room_id")
    try:
        room_id = int(room_id)
    except (TypeError, ValueError):
        return None
    if room_id <= 0:
        return None
    return {
        "room_id": room_id,
        "uid": int(raw.get("uid") or 0),
        "uname": str(raw.get("uname") or "").strip(),
        "title": str(raw.get("title") or "").strip(),
        # 1=直播中 2=轮播中 0=未开播
        "live_status": int(raw.get("live_status") or 0),
        "face": str(raw.get("face") or "").strip(),
        "live_time": str(raw.get("live_time") or "").strip(),
    }


def fetch_followed_page(sessdata: str, page: int = 1) -> dict:
    """拉取关注列表的单页数据（前端增量加载的数据源）。

    返回：{items, page, page_size, total, total_page, live_count}
    Raises FollowingError: 未登录 / 业务错误 / 网络失败 / 页码非法。
    """
    sessdata = (sessdata or "").strip()
    if not sessdata:
        raise FollowingError("未配置 SESSDATA", -101)
    try:
        page = int(page)
    except (TypeError, ValueError):
        page = 0
    if not 1 <= page <= MAX_PAGE:
        raise FollowingError(f"页码非法：{page}", 400)

    with httpx.Client(
        headers=P.HEADERS,
        cookies={"SESSDATA": sessdata},
        timeout=15, follow_redirects=True,
    ) as client:
        # 必须先拿风控 cookie，缺 buvid3 时分页参数会被服务端静默忽略
        P.init_risk_cookies(client)
        try:
            resp = client.get(FOLLOWING_URL, params={
                "page": page, "page_size": PAGE_SIZE,
            })
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise FollowingError(f"关注列表请求失败：{e}") from e

    code = body.get("code")
    if code != 0:
        if code in (-101, -111):
            raise FollowingError(
                "登录态无效或已过期，请重新到「设置」页填写 SESSDATA",
                code)
        raise FollowingError(
            f"关注列表返回错误（code={code}）："
            f"{str(body.get('message', ''))[:80]}", code)

    data = body.get("data") or {}
    raw_items = data.get("list") or data.get("rooms") or []
    items: list[dict] = []
    seen: set[int] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        it = _norm_item(raw)
        if it and it["room_id"] not in seen:
            seen.add(it["room_id"])
            items.append(it)

    total = int(data.get("count") or 0)
    total_page = int(data.get("total_page")
                     or data.get("totalPage") or 0)
    live_count = int(data.get("live_count") or 0)
    return {
        "items": items,
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
        "total_page": total_page,
        "live_count": live_count,
    }
