"""
STRM 文件命名与媒体信息解析
"""
from __future__ import annotations

import json
import os
import re

# 来源标签：从旧文件名识别，并统一规范化写法
SOURCE_PATTERNS: list[tuple[str, str]] = [
    (r"WEB[.\-_ ]?DL", "WEB-DL"),
    (r"WEB[.\-_ ]?RIP", "WEBRip"),
    (r"BLU[.\-_ ]?RAY", "BluRay"),
    (r"REMUX", "REMUX"),
    (r"HDTV", "HDTV"),
    (r"DVD[.\-_ ]?RIP", "DVDRip"),
]

NOT_GROUP_TOKENS = {
    "WEB", "WEB-DL", "WEBDL", "WEBRIP", "BLURAY", "REMUX", "HDTV", "DVDRIP",
    "DL", "RIP",
    "HDR", "HDR10", "HDR10+", "SDR", "HLG", "DV", "DOVI", "HEVC", "H264", "X264", "H265", "X265", "AVC",
}

DELIM_CLASS = r"[.\-\s_\[\](){}]"


def extract_source_tag(name_part: str) -> str:
    for pat, norm in SOURCE_PATTERNS:
        if re.search(rf"(?i)(^|{DELIM_CLASS}){pat}(?=({DELIM_CLASS}|$))", name_part):
            return norm
    return ""


def extract_release_group(name_part: str) -> tuple[str, str]:
    m = re.search(r"-(?P<grp>[A-Za-z0-9][A-Za-z0-9]{1,30})$", name_part)
    if not m:
        return name_part, ""
    grp = m.group("grp")
    if grp.upper() in NOT_GROUP_TOKENS:
        return name_part, ""
    return name_part[: m.start()], "-" + grp


def cleanup_body(s: str) -> str:
    s = s.replace("[]", "").replace("()", "").replace("{}", "")
    s = re.sub(r"[.\-\s_]{2,}", ".", s)
    return s.strip(".-_ ")


def build_wipe_regex(info: dict, has_source: bool) -> re.Pattern | None:
    alts: list[str] = []

    if info.get("res"):
        alts.append(r"2160p|1080p|720p|1080i|720i|4k|8k|UHD")
    if info.get("fps"):
        alts.append(r"\d+(?:\.\d+)?fps")
    if info.get("hdr"):
        alts.append(r"SDR|HDR10\+|HDR10|HDR|HLG|Dolby[\s.\-_]?Vision|DoVi|\bDV\b|DVP\d")
    if info.get("v_codec"):
        alts.append(r"H\.?264|X\.?264|AVC|H\.?265|X\.?265|HEVC|AV1")
    if info.get("a_codec"):
        alts.append(r"TrueHD[\s.\-_]?Atmos|Dolby[\s.\-_]?Atmos|Atmos")
        alts.append(r"DTS-HD[\s.\-_]?(?:MA|HRA)?[\s.\-_]?[1257]\.[01]")
        alts.append(r"DTS[\s.\-_]?[1257]\.[01]")
        alts.append(r"DDP[\s.\-_]?[1257]\.[01]|EAC3[\s.\-_]?[1257]\.[01]")
        alts.append(r"DD\+[\s.\-_]?[1257]\.[01]|AC3[\s.\-_]?[1257]\.[01]|DD[\s.\-_]?[1257]\.[01]")
        alts.append(r"TrueHD[\s.\-_]?[1257]\.[01]")
        alts.append(r"AAC[\s.\-_]?[1257]\.[01]|FLAC[\s.\-_]?[1257]\.[01]|OPUS[\s.\-_]?[1257]\.[01]")
        alts.append(r"DTS-HD[\s.\-_]?(?:MA|HRA)?|DTS")
        alts.append(r"DDP|EAC3|DD\+|AC3|TrueHD|Dolby|AAC|FLAC|OPUS")
        alts.append(r"[1257]\.[01]")
    if info.get("depth"):
        alts.append(r"(?:8|10|12|14|16)bit")

    if has_source:
        alts.extend([p for p, _ in SOURCE_PATTERNS])

    if not alts:
        return None

    return re.compile(rf"(?i)(^|{DELIM_CLASS})(?:" + "|".join(alts) + rf")(?=({DELIM_CLASS}|$))")


def wipe_tags(main_body: str, info: dict, has_source: bool) -> str:
    wre = build_wipe_regex(info, has_source)
    if not wre:
        return cleanup_body(main_body)

    prev = None
    cur = main_body
    for _ in range(5):
        if prev == cur:
            break
        prev = cur
        cur = wre.sub(r"\1", cur)
        cur = cleanup_body(cur)
    return cur


def parse_fps(v_stream: dict) -> str:
    v = v_stream.get("avg_frame_rate") or v_stream.get("r_frame_rate") or "0/0"
    if not isinstance(v, str) or "/" not in v:
        return ""
    try:
        n_s, d_s = v.split("/", 1)
        n, d = int(n_s), int(d_s)
        if d == 0 or n == 0:
            return ""
        fps = n / d
    except Exception:
        return ""

    common = [23.976, 24.000, 25.000, 29.970, 30.000, 50.000, 59.940, 60.000, 119.880, 120.000]
    for c in common:
        if abs(fps - c) < 0.02:
            fps = c
            break

    if abs(fps - round(fps)) < 0.001:
        return f"{int(round(fps))}fps"
    return f"{fps:.3f}fps"


def parse_bit_depth(v_stream: dict) -> str:
    pix = (v_stream.get("pix_fmt") or "").lower()
    m = re.search(r"(?i)(8|10|12|14|16)(?=le|be)", pix)
    if m:
        return f"{m.group(1)}bit"

    b = v_stream.get("bits_per_raw_sample")
    if isinstance(b, str) and b.isdigit():
        return f"{b}bit"
    if isinstance(b, int) and b > 0:
        return f"{b}bit"

    if "12" in pix:
        return "12bit"
    if "10" in pix:
        return "10bit"
    return "8bit"


def parse_hdr(v_stream: dict) -> str:
    hdr_tags: list[str] = []

    side = v_stream.get("side_data_list") or []
    if isinstance(side, list):
        dovi = next((sd for sd in side if "DOVI" in str(sd.get("side_data_type", "")).upper()), None)
        if dovi:
            prof = dovi.get("dv_profile")
            hdr_tags.append(f"DVP{prof}" if isinstance(prof, int) else "DV")

    tr = (v_stream.get("color_transfer") or "").lower()
    if tr == "arib-std-b67":
        hdr_tags.append("HLG")
    elif tr == "smpte2084":
        is_hdr10p = False
        if isinstance(side, list):
            for sd in side:
                s = json.dumps(sd, ensure_ascii=False)
                if "HDR Dynamic Metadata" in s or "dynamic_hdr_plus" in s.lower() or "hdr10+" in s.lower():
                    is_hdr10p = True
                    break
        hdr_tags.append("HDR10+" if is_hdr10p else "HDR10")

    return ".".join(hdr_tags) if hdr_tags else "SDR"


def choose_audio_stream(streams: list[dict]) -> dict | None:
    a = [s for s in streams if s.get("codec_type") == "audio"]
    if not a:
        return None

    def score(s: dict) -> tuple[int, int, int]:
        disp = s.get("disposition") or {}
        is_def = 1 if disp.get("default") == 1 else 0
        ch = s.get("channels") or 0
        br = s.get("bit_rate") or 0
        try:
            br = int(br)
        except Exception:
            br = 0
        return (is_def, ch, br)

    return sorted(a, key=score, reverse=True)[0]


def parse_audio(a_stream: dict, fmt: dict) -> str:
    codec = (a_stream.get("codec_name") or "").upper()
    ch_n = a_stream.get("channels") or 2
    ch_map = {8: "7.1", 6: "5.1", 2: "2.0", 1: "1.0"}
    ch = ch_map.get(ch_n, f"{ch_n}ch")

    base_map = {
        "EAC3": "DDP",
        "AC3": "DD",
        "TRUEHD": "TrueHD",
        "DTS": "DTS",
        "AAC": "AAC",
        "FLAC": "FLAC",
        "OPUS": "Opus",
    }
    base = base_map.get(codec, codec or "")

    blob = [str(a_stream.get("profile", ""))]
    tags = a_stream.get("tags") or {}
    if isinstance(tags, dict):
        blob.extend([str(v) for v in tags.values()])
    ft = fmt.get("tags") or {}
    if isinstance(ft, dict):
        blob.extend([str(v) for v in ft.values()])
    blob_s = " ".join(blob).lower()
    atmos = "Atmos" if ("atmos" in blob_s or "joc" in blob_s) else ""

    return ".".join([t for t in (base, ch, atmos) if t])


def parse_media_info(data: dict) -> dict:
    info = {"res": "", "fps": "", "hdr": "", "v_codec": "", "a_codec": "", "depth": ""}
    if not data or "streams" not in data:
        return info

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v:
        h = v.get("height") or 0
        try:
            h = int(h)
        except Exception:
            h = 0

        if h >= 2160:
            info["res"] = "2160p"
        elif h >= 1080:
            info["res"] = "1080p"
        elif h >= 720:
            info["res"] = "720p"
        elif h > 0:
            info["res"] = f"{h}p"

        vc = (v.get("codec_name") or "").lower()
        info["v_codec"] = {"hevc": "HEVC", "h264": "AVC", "av1": "AV1"}.get(vc, vc.upper())
        info["fps"] = parse_fps(v)
        info["depth"] = parse_bit_depth(v)
        info["hdr"] = parse_hdr(v)

    a = choose_audio_stream(streams)
    if a:
        info["a_codec"] = parse_audio(a, fmt)

    return info


def generate_new_name(old_name: str, info: dict) -> str:
    name_part, ext = os.path.splitext(old_name)
    main_body, group = extract_release_group(name_part)
    source = extract_source_tag(main_body)
    clean = wipe_tags(main_body, info=info, has_source=bool(source))

    tags_order = [
        info.get("res", ""),
        source,
        info.get("fps", ""),
        info.get("hdr", ""),
        info.get("v_codec", ""),
        info.get("a_codec", ""),
        info.get("depth", ""),
    ]
    seg = ".".join([t for t in tags_order if t])

    new_name = f"{clean}.{seg}{group}{ext}" if seg else f"{clean}{group}{ext}"
    new_name = re.sub(r"\.{2,}", ".", new_name).replace(".-", "-").replace("-.", "-")
    return new_name
