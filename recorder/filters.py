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
    return bool(text) and not RE_EMOTICON.sub("", text).strip()


def sanitize_text(text: str) -> str:
    text = RE_URL.sub("【链接】", text)
    text = RE_MENTION.sub("@【用户】", text)
    text = RE_QQ.sub("【QQ号】", text)
    text = RE_UID.sub("【UID】", text)
    text = RE_PHONE.sub("【手机号】", text)
    return text.strip()


def anon_id(real_id: int, salt: str) -> str:
    return hashlib.sha256(f"{real_id}:{salt}".encode()).hexdigest()[:16]


def anon_name(uname: str) -> str:
    """用户名掩码：保留风格但去识别性。"""
    if not uname:
        return ""
    return uname[:1] + "***" if len(uname) > 1 else uname + "***"
