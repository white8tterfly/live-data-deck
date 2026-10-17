"""FLV 头部元数据零依赖探测：视频像素分辨率（宽 / 高）。

生成 ASS 字幕时需要知道真实视频尺寸（PlayResX/Y 要与视频一致，字号/车道/速度
才能与原生观感对齐），但 ffmpeg 不随程序内置，不能强依赖外部进程，于是改为直接
解析 FLV 字节流头部：

1. script tag（onMetaData，AMF0）里的 width/height（多数 CDN 转码流带）；
2. 没有则解析首个视频 tag 的 AVC sequence header（AVCDecoderConfigurationRecord）
   中第一个 SPS，去 emulation prevention 后用 exp-Golomb 解出帧宽高
   （含 frame_crop 裁剪）；
3. HEVC（codecId=12）的 SPS 结构复杂，P0 不解析，返回 None 走 ffmpeg/默认兜底；
4. 数据损坏 / 截断一律返回 None，不抛异常（探测失败不得影响录制与字幕生成）。

参考规范：Adobe FLV/F4V 规范、ISO 14496-10（H.264）7.3.2.1 SPS 语法。
"""
from __future__ import annotations

from pathlib import Path

FLV_HEADER_LEN = 9
PREV_TAG_LEN = 4
TAG_HEADER_LEN = 11
MAX_SCAN_BYTES = 262144      # 只读文件头 256KB（script tag 与 sequence header 均在流最前面）
MAX_SCAN_TAGS = 32

TAG_SCRIPT = 18
TAG_VIDEO = 9
CODEC_AVC = 7


def probe_flv_file(path: str | Path, head_bytes: int = MAX_SCAN_BYTES) -> tuple[int, int] | None:
    """读取 FLV 文件头部并探测 (宽, 高)；失败返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read(head_bytes)
    except OSError:
        return None
    return probe_flv_dims(data)


def probe_flv_dims(data: bytes) -> tuple[int, int] | None:
    """从 FLV 字节流（含完整头部即可）探测 (宽, 高)。"""
    try:
        if len(data) < FLV_HEADER_LEN + PREV_TAG_LEN or data[:3] != b"FLV":
            return None
        pos = FLV_HEADER_LEN + PREV_TAG_LEN
        tags_seen = 0
        while pos + TAG_HEADER_LEN <= len(data) and tags_seen < MAX_SCAN_TAGS:
            tag_type = data[pos]
            data_size = int.from_bytes(data[pos + 1:pos + 4], "big")
            body_start = pos + TAG_HEADER_LEN
            body_end = body_start + data_size
            if body_end > len(data):
                break  # 头部截断（读到的字节不完整），停止扫描
            body = data[body_start:body_end]
            tags_seen += 1
            if tag_type == TAG_SCRIPT:
                dims = _dims_from_amf(body)
                if dims:
                    return dims
            elif tag_type == TAG_VIDEO and data_size >= 5:
                codec_id = body[0] & 0x0F
                avc_pkt_type = body[1]
                if codec_id == CODEC_AVC and avc_pkt_type == 0:
                    dims = _dims_from_avc_config(body[5:])
                    if dims:
                        return dims
            pos = body_end + PREV_TAG_LEN
    except (IndexError, ValueError, OverflowError):
        return None
    return None


# AMF0 onMetaData

def _dims_from_amf(body: bytes) -> tuple[int, int] | None:
    """从 script tag body（AMF0）找 onMetaData 的 width/height（double）。"""
    try:
        amf = _AMF0(body)
        found: dict[str, float] = {}
        # script tag 通常是 ["onMetaData", {...}]，部分推流端前面还多一个
        # "@setDataFrame" 字符串；最多顺读 4 个顶层值，读到尺寸即止
        for _ in range(4):
            amf.parse_value(collect=found, depth=0)
            if found.get("width") and found.get("height"):
                break
    except (_AMFError, IndexError, ValueError):
        if not (_valid_dims(found.get("width", 0), found.get("height", 0))):
            return None
    try:
        w = float(found.get("width", 0))
        h = float(found.get("height", 0))
    except (TypeError, ValueError):
        return None
    if _valid_dims(w, h):
        return int(w), int(h)
    return None


class _AMFError(Exception):
    pass


class _AMF0:
    """最小 AMF0 解码：只需要在 onMetaData 对象里收集 width/height 数值。"""

    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self) -> int:
        v = int.from_bytes(self.d[self.p:self.p + 2], "big")
        self.p += 2
        return v

    def u32(self) -> int:
        v = int.from_bytes(self.d[self.p:self.p + 4], "big")
        self.p += 4
        return v

    def double(self) -> float:
        import struct
        v = struct.unpack(">d", self.d[self.p:self.p + 8])[0]
        self.p += 8
        return v

    def string(self, long: bool = False) -> str:
        n = self.u32() if long else self.u16()
        s = self.d[self.p:self.p + n].decode("utf-8", errors="replace")
        self.p += n
        return s

    def parse_value(self, collect: dict | None = None, depth: int = 0,
                    key: str = "") -> None:
        if depth > 6 or self.p >= len(self.d):
            raise _AMFError("too deep or truncated")
        marker = self.u8()
        if marker == 0x00:       # Number
            val = self.double()
            if collect is not None and key in ("width", "height"):
                collect[key] = val
        elif marker == 0x01:     # Boolean
            self.p += 1
        elif marker == 0x02:     # String
            self.string()
        elif marker in (0x03, 0x08, 0x0A):
            # Object / ECMA Array / Strict Array —— 本任务只关心 Object/ECMA 的命名字段
            if marker == 0x08:
                self.u32()       # associative count（提示值，不可信）
            elif marker == 0x0A:
                count = self.u32()
                for _ in range(min(count, 256)):
                    self.parse_value(collect, depth + 1)
                return
            self._parse_object_body(collect, depth)
        elif marker == 0x0C:     # LongString
            self.string(long=True)
        elif marker in (0x05, 0x06, 0x0D):  # null / undefined / unsupported
            pass
        elif marker == 0x07:     # Reference
            self.u16()
        elif marker == 0x0B:     # Date
            self.p += 10
        else:
            raise _AMFError(f"unsupported amf marker {marker:#x}")

    def _parse_object_body(self, collect: dict | None, depth: int) -> None:
        guard = 0
        while self.p < len(self.d) and guard < 512:
            name_len = self.u16()
            if name_len == 0:
                end = self.u8()   # 0x09 Object End
                if end == 0x09:
                    return
                raise _AMFError("bad object terminator")
            name = self.d[self.p:self.p + name_len].decode(
                "utf-8", errors="replace")
            self.p += name_len
            self.parse_value(collect, depth + 1, key=name)
            guard += 1
        raise _AMFError("object not terminated")


# H.264 SPS

def _dims_from_avc_config(payload: bytes) -> tuple[int, int] | None:
    """解析 AVCDecoderConfigurationRecord 中第一个 SPS 得到 (宽, 高)。"""
    if len(payload) < 7:
        return None
    # [0]version [1]profile [2]compat [3]level [4]lengthSize-1 [5]numSPS
    num_sps = payload[5] & 0x1F
    p = 6
    for _ in range(num_sps or 1):
        if p + 2 > len(payload):
            return None
        sps_len = int.from_bytes(payload[p:p + 2], "big")
        p += 2
        if sps_len <= 0 or p + sps_len > len(payload):
            return None
        nal = payload[p:p + sps_len]
        p += sps_len
        if (nal[0] & 0x1F) == 7:
            dims = _dims_from_sps_nal(nal)
            if dims:
                return dims
    return None


def _rbsp(nal: bytes) -> bytes:
    """去掉 NAL 的 emulation prevention byte（00 00 03 → 00 00）。首字节为 NAL header。"""
    out = bytearray([nal[0]])
    i = 1
    zeros = 0
    while i < len(nal):
        b = nal[i]
        if zeros >= 2 and b == 0x03 and i + 1 < len(nal):
            i += 1  # 丢弃防竞争字节
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
        i += 1
    return bytes(out)


def _dims_from_sps_nal(nal: bytes) -> tuple[int, int] | None:
    """从单个 SPS NAL 用 exp-Golomb 解出帧宽高（含 crop 修正）；失败返回 None。"""
    try:
        rbsp = _rbsp(nal)
        br = _BitReader(rbsp)
        br.u(8)  # NAL header（forbidden_zero_bit / nal_ref_idc / nal_unit_type）
        profile_idc = br.u(8)
        br.u(8)   # constraint flags + reserved
        br.u(8)   # level_idc
        br.ue()   # sps_id
        if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128,
                           138, 139, 134, 135):
            chroma = br.ue()
            if chroma == 3:
                br.u(1)  # separate_colour_plane_flag
            br.ue()  # bit_depth_luma_minus8
            br.ue()  # bit_depth_chroma_minus8
            br.u(1)  # qpprime_y_zero_transform_bypass_flag
            if br.u(1):  # seq_scaling_matrix_present_flag
                lists = 12 if chroma == 3 else 8
                for i in range(lists):
                    if br.u(1):
                        _skip_scaling_list(br, 16 if i < 6 else 64)
        br.ue()  # log2_max_frame_num_minus4
        poc_type = br.ue()
        if poc_type == 0:
            br.ue()  # log2_max_pic_order_cnt_lsb_minus4
        elif poc_type == 1:
            br.u(1)  # delta_pic_order_always_zero_flag
            br.se()  # offset_for_non_ref_pic
            br.se()  # offset_for_top_to_bottom_field
            n = br.ue()
            for _ in range(min(n, 256)):
                br.se()
        br.ue()  # max_num_ref_frames
        br.u(1)  # gaps_in_frame_num_value_allowed_flag
        width_mbs = br.ue() + 1
        height_map = br.ue() + 1
        frame_mbs_only = br.u(1)
        if not frame_mbs_only:
            br.u(1)  # mb_adaptive_frame_field_flag
        br.u(1)  # direct_8x8_inference_flag
        crop_l = crop_r = crop_t = crop_b = 0
        if br.u(1):  # frame_cropping_flag
            crop_l = br.ue()
            crop_r = br.ue()
            crop_t = br.ue()
            crop_b = br.ue()
        # crop 单位对 4:2:0 色度采样：水平 ×2、垂直 ×2×(2-frame_mbs_only)
        width = width_mbs * 16 - (crop_l + crop_r) * 2
        height = (2 - frame_mbs_only) * height_map * 16 \
            - (crop_t + crop_b) * 2 * (2 - frame_mbs_only)
        if not _valid_dims(width, height):
            return None
        return width, height
    except (_BitError, IndexError, ValueError):
        return None


def _skip_scaling_list(br: "_BitReader", size: int) -> None:
    """按 H.264 规则跳过一段 scaling list（只推进位游标，不取值）。"""
    last = next_scale = 8
    for _ in range(size):
        if next_scale != 0:
            delta = br.se()
            next_scale = (last + delta + 256) % 256
        if next_scale != 0:
            last = next_scale


class _BitError(Exception):
    pass


class _BitReader:
    """沿 NAL RBSP 逐位读：u(n) 定长、ue/se 指数哥伦布；越界抛 _BitError。"""

    def __init__(self, data: bytes) -> None:
        self.d = data
        self.byte_i = 0
        self.bit_i = 0

    def u(self, n: int) -> int:
        """读 n 位无符号整数（MSB 优先）。"""
        v = 0
        for _ in range(n):
            if self.byte_i >= len(self.d):
                raise _BitError("truncated")
            v = (v << 1) | ((self.d[self.byte_i] >> (7 - self.bit_i)) & 1)
            self.bit_i += 1
            if self.bit_i == 8:
                self.bit_i = 0
                self.byte_i += 1
        return v

    def ue(self) -> int:
        """无符号指数哥伦布 ue(v)。"""
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 32:
                raise _BitError("ue too long")
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        """有符号指数哥伦布 se(v)。"""
        code = self.ue()
        if code % 2 == 0:
            return -(code // 2)
        return (code + 1) // 2


def _valid_dims(w: float, h: float) -> bool:
    """宽高是否为合理正整数：排除 0 / NaN / Inf 与超范围值，允许竖屏。"""
    try:
        w, h = float(w), float(h)
    except (TypeError, ValueError):
        return False
    # 排除 0、NaN、Inf 与离谱值；竖屏直播（如 1080x1920）也允许
    return (160 <= w <= 7680 and 160 <= h <= 4320
            and w == int(w) and h == int(h))
