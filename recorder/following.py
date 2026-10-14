"""B 站直播关注列表：读取「我关注的主播」及其开播状态（需要登录态）。

接口 /xlive/web-ucenter/user/following 是直播中心「我的关注」数据源，
返回当前账号关注的全部直播间（开播的排最前），游客调用返回 -101。
与弹幕通道不同：弹幕 WS 游客可连，关注列表属于私有数据，必须带有效 SESSDATA。
"""
from __future__ import annotations

import httpx

from . import protocol as P

FOLLOWING_URL = ("https://api.live.bilibili.com/xlive/web-ucenter/"
                 "user/following")
PAGE_SIZE = 50
MAX_PAGES = 20          # 最多拉 1000 个关注，防止异常分页死循环


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


def fetch_all_followed(sessdata: str) -> list[dict]:
    """翻页拉取全部关注直播间，开播的排最前（保持接口顺序并兜底排序）。

    Raises FollowingError: 未登录 / 业务错误 / 网络失败。
    """
    sessdata = (sessdata or "").strip()
    if not sessdata:
        raise FollowingError("未配置 SESSDATA", -101)

    items: list[dict] = []
    seen: set[int] = set()
    with httpx.Client(
        headers=P.HEADERS,
        cookies={"SESSDATA": sessdata},
        timeout=15, follow_redirects=True,
    ) as client:
        P.init_risk_cookies(client)
        total_page = 0
        for page in range(1, MAX_PAGES + 1):
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
            page_items = data.get("list") or data.get("rooms") or []
            for raw in page_items:
                if not isinstance(raw, dict):
                    continue
                it = _norm_item(raw)
                if it and it["room_id"] not in seen:
                    seen.add(it["room_id"])
                    items.append(it)

            # 分页字段各端不一致，做防御式处理
            if not total_page:
                total_page = int(data.get("total_page")
                                 or data.get("totalPage") or 0)
            if total_page and page >= total_page:
                break
            if not page_items:
                break
            if data.get("has_more") is False:
                break

    # 接口本身开播在前，这里兜底：直播中 > 轮播 > 未开播，组内保持原顺序
    order = {1: 0, 2: 1}
    items.sort(key=lambda x: order.get(x["live_status"], 2))
    return items
