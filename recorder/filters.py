"""文本过滤与隐私脱敏。

- is_pure_emoticon: 纯大表情弹幕识别（剥除 [xxx] 后无任何文字）
- sanitize_text:    链接 / @提及 / QQ / UID / 手机号脱敏
- anon_id:          uid 哈希匿名（sha256 加盐截断，跨文件可关联、不可逆）
- anon_name:        用户名掩码（保留首字风格，去除识别性）
"""
from __future__ import annotations

import hashlib
import re

# B 站大表情 / 粉丝牌表情在弹幕文本里是 [名称] 占位
RE_EMOTICON = re.compile(r"\[[^\[\]]{1,40}\]")

RE_URL = re.compile(r"https?://\S+|www\.\S+")
RE_MENTION = re.compile(r"@[^\s@，。,~！!？?]{1,20}")
RE_QQ = re.compile(r"(?:qq|QQ|企鹅|扣扣)[号:：\s]*\d{5,11}")
RE_UID = re.compile(r"(?:uid|UID|Uid)[号:：\s]*\d{4,10}")
RE_PHONE = re.compile(r"1[3-9]\d{9}")


def is_pure_emoticon(text: str) -> bool:
    """文本剥掉 [表情] 占位后是否只剩空白，即纯大表情弹幕。"""
    return bool(text) and not RE_EMOTICON.sub("", text).strip()


def sanitize_text(text: str) -> str:
    """把链接 / @提及 / QQ / UID / 手机号替换成占位标记后返回。"""
    text = RE_URL.sub("【链接】", text)
    text = RE_MENTION.sub("@【用户】", text)
    text = RE_QQ.sub("【QQ号】", text)
    text = RE_UID.sub("【UID】", text)
    text = RE_PHONE.sub("【手机号】", text)
    return text.strip()


def anon_id(real_id: int, salt: str) -> str:
    """uid 加盐 sha256 取前 16 位十六进制：跨文件稳定，且不可反推真实 uid。"""
    return hashlib.sha256(f"{real_id}:{salt}".encode()).hexdigest()[:16]


def anon_name(uname: str) -> str:
    """用户名掩码：保留风格但去识别性。"""
    if not uname:
        return ""
    return uname[:1] + "***" if len(uname) > 1 else uname + "***"


def record_user_key(rec: dict) -> str:
    """录制记录的身份键：uid（真实/匿名）优先；uid 缺失（uid=0 游客弹幕）
    时退回掩码用户名，避免把所有游客的相同文案误并为同一人。
    无法确定身份时返回空串（调用方应跳过用户维度统计/去重）。
    """
    uid = str(rec.get("uid_anon") or rec.get("uid") or "").strip()
    if uid:
        return uid
    uname = str(rec.get("uname_anon") or "").strip()
    return f"名:{uname}" if uname else ""
