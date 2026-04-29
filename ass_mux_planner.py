from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ass_mux_config import AssMuxSettings
from ass_utils import AssPipelineError, ensure_dir


@dataclass(slots=True)
class SubtitleTrackPlan:
    file: str
    group: str
    lang_raw: str
    mkv_lang: str
    track_name: str


@dataclass(slots=True)
class MuxPlanItem:
    mkv: str
    subs: list[SubtitleTrackPlan]


@dataclass(slots=True)
class MuxPlan:
    generated_at: str
    target_dir: str
    defaults: dict[str, str]
    items: list[MuxPlanItem]
    total_mkvs: int
    matched_mkvs: int
    total_sub_tracks: int


def get_ep_num(name: str) -> str:
    match = re.search(r"S\d{1,2}E(\d{1,3})", name, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    brackets = re.findall(r"\[(\d{1,3})\]", name)
    for item in brackets:
        if item not in ("720", "1080", "1920", "2160", "264", "265"):
            return str(int(item))

    match = re.search(r"(?:-|–)\s*(\d{2,3})\s*(?:v\d|\[|\.)", name, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    match = re.search(r"EP(\d{1,3})", name, re.IGNORECASE)
    if match:
        return str(int(match.group(1)))

    match = re.search(r"\s(\d{2})\s", name)
    if match:
        return str(int(match.group(1)))

    return ""


def short_ep_display(name: str) -> str:
    num = get_ep_num(name)
    if num:
        return f"E{num.zfill(2)}"
    return ""


def short_title_from_mkv(name: str) -> str:
    match = re.search(r"^(.+?\(\d{4}\))\s*-\s*S\d{2}E\d{2}", name)
    if match:
        return match.group(1)

    match = re.search(r"^(.+?)\s*-\s*S\d{2}E\d{2}", name)
    if match:
        return match.group(1).strip()

    clean = re.sub(r"^\[.*?\]\s*", "", name)
    match = re.search(r"^(.+?)(\[|\s-\s|\s\d{2})", clean)
    if match:
        return match.group(1).replace(".", " ").strip()

    return clean[:20]


def parse_lang(raw: str) -> tuple[str, str]:
    text = (raw or "").lower().strip().replace("&", "_").replace("+", "_").replace("-", "_")
    parts = [item for item in text.split("_") if item]

    lang_map = {
        "chs": ("zh-Hans", "简"), "sc": ("zh-Hans", "简"), "zh": ("zh-Hans", "简"), "cn": ("zh-Hans", "简"),
        "cht": ("zh-Hant", "繁"), "tc": ("zh-Hant", "繁"), "tw": ("zh-Hant", "繁"), "hk": ("zh-Hant", "繁"),
        "jpn": ("ja", "日"), "ja": ("ja", "日"), "jp": ("ja", "日"),
        "eng": ("en", "英"), "en": ("en", "英"),
        "kor": ("ko", "韩"), "kr": ("ko", "韩"), "ko": ("ko", "韩"),
        "fre": ("fr", "法"), "fra": ("fr", "法"), "fr": ("fr", "法"),
        "ger": ("de", "德"), "deu": ("de", "德"), "de": ("de", "德"),
        "spa": ("es", "西"), "es": ("es", "西"),
        "rus": ("ru", "俄"), "ru": ("ru", "俄"),
        "tha": ("th", "泰"), "th": ("th", "泰"),
        "vie": ("vi", "越"), "vi": ("vi", "越"),
        "ara": ("ar", "阿"), "ar": ("ar", "阿"),
    }

    matched: list[tuple[str, str]] = []
    for part in parts:
        item = lang_map.get(part)
        if item and item not in matched:
            matched.append(item)

    if not matched:
        return "und", "未知"

    if len(matched) == 1:
        mkv_code, short_name = matched[0]
        if short_name in ("简", "繁"):
            return mkv_code, short_name + "中"
        return mkv_code, short_name + "语"

    mkv_code = matched[0][0]
    return mkv_code, "".join(item[1] for item in matched)


def build_track_name(group: str, lang_raw: str) -> tuple[str, str]:
    mkv_lang, lang_cn = parse_lang(lang_raw)
    track_name = f"{group} | {lang_cn}" if group else lang_cn
    return mkv_lang, track_name


def infer_lang_raw_from_subtitle_name(name: str, fallback: str) -> str:
    stem = Path(name).stem
    token_parts = [part for part in re.split(r"[\s._\-+/&|／＋＆｜]+", stem) if part]
    bracket_parts = re.findall(r"[\[\(【（]([^\]\)】）]{1,20})[\]\)】）]", stem)

    candidates: list[str] = []

    def _push(text: str) -> None:
        if text and text not in candidates:
            candidates.append(text)

    # 优先检查：括号整体片段、完整 stem、相邻 token 组合
    for part in reversed(bracket_parts):
        _push(part)
    _push(stem)

    for size in (3, 2):
        if len(token_parts) >= size:
            for start in range(len(token_parts) - size + 1):
                _push(''.join(token_parts[start:start + size]))
                _push('_'.join(token_parts[start:start + size]))

    # 最后才回退到单 token，避免 JPTC / CHS&JPN 先被识别成单语
    for part in reversed(token_parts):
        _push(part)

    def _normalize(text: str) -> str:
        return re.sub(r"[\s._\-\[\]\(\)【】（）&+|/／＋＆｜]+", "", text).lower()

    def _detect(segment: str) -> str | None:
        raw = segment.strip()
        if not raw:
            return None
        s = _normalize(raw)
        if not s:
            return None

        explicit_pairs = [
            ("chs_eng", [
                "简英", "英简", "chseng", "engchs", "sceng", "engsc", "ensc", "scen",
                "zhhanseng", "engzhhans", "zhcneng", "engzhcn", "中英双语", "双语简英",
            ]),
            ("cht_eng", [
                "繁英", "英繁", "chteng", "engcht", "tceng", "engtc", "entc", "tcen",
                "big5eng", "engbig5", "zhhanteng", "engzhhant", "zhtweng", "engzhtw", "繁英双语",
            ]),
            ("chs_jpn", [
                "简日", "日简", "chsjpn", "jpnchs", "chsjp", "jpchs", "jpsc", "scjp", "jpsc字幕",
                "zhhansjpn", "jpnzhhans", "zhcnjpn", "jpnzhcn", "简日双语",
            ]),
            ("cht_jpn", [
                "繁日", "日繁", "chtjpn", "jpncht", "chtjp", "jpcht", "jptc", "tcjp", "jptc字幕",
                "big5jp", "jpbig5", "zhhantjpn", "jpnzhhant", "zhtwjpn", "jpnzhtw", "繁日双语",
            ]),
        ]
        for lang_raw, patterns in explicit_pairs:
            if any(p in s for p in patterns):
                return lang_raw

        short = len(s) <= 20
        if short and any(p in s for p in ["简中", "简体", "chs", "gb", "gbk", "gb2312", "zhhans", "zhcn", "zhchs", "简"]):
            return "chs"
        if short and any(p in s for p in ["繁中", "繁體", "繁体", "cht", "big5", "zhhant", "zhtw", "zhhk", "繁"]):
            return "cht"
        if short and any(p in s for p in ["日文", "日语", "jpn", "japanese", "jap", "ja", "jp", "日"]):
            return "jpn"
        if short and any(p in s for p in ["英文", "英语", "eng", "english", "enus", "英"]):
            return "eng"
        return None

    for candidate in candidates:
        detected = _detect(candidate)
        if detected:
            return detected
    return fallback


def _iter_files(root: Path, recursive: bool, suffixes: tuple[str, ...]) -> list[Path]:
    iterator = root.rglob("*") if recursive else root.glob("*")
    return sorted(
        path for path in iterator
        if path.is_file() and path.suffix.lower() in suffixes
    )


def _normalize_sub_identity(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".assfonts.ass"):
        return name[:-len(".assfonts.ass")] + ".ass"
    return name


def _dedupe_subs(paths: list[Path]) -> list[Path]:
    chosen: dict[str, Path] = {}
    for path in paths:
        key = _normalize_sub_identity(path)
        prev = chosen.get(key)
        if not prev:
            chosen[key] = path
            continue
        prev_is_generated = prev.name.lower().endswith(".assfonts.ass")
        curr_is_generated = path.name.lower().endswith(".assfonts.ass")
        if curr_is_generated and not prev_is_generated:
            chosen[key] = path
    return sorted(chosen.values(), key=lambda p: p.name.lower())


def find_subs_for_mkv(mkv: Path, subs: list[Path]) -> list[Path]:
    if not subs:
        return []

    ep_mkv = get_ep_num(mkv.name)
    mkv_stem_lower = mkv.stem.lower()

    if ep_mkv:
        hits = [sub for sub in subs if get_ep_num(sub.name) == ep_mkv]
        if hits:
            hits = _dedupe_subs(hits)
            return sorted(hits, key=lambda p: (abs(len(p.stem.lower()) - len(mkv_stem_lower)), p.name.lower()))

    hits = [sub for sub in subs if mkv_stem_lower in sub.name.lower()]
    hits = _dedupe_subs(hits)
    return sorted(hits, key=lambda p: (abs(len(p.stem.lower()) - len(mkv_stem_lower)), p.name.lower()))


def build_mux_plan(settings: AssMuxSettings, *, default_group: str | None = None, default_lang: str | None = None) -> MuxPlan:
    target_dir = settings.target_dir.expanduser().resolve()
    if not target_dir.is_dir():
        raise AssPipelineError(f"ASS_MUX_TARGET_DIR 不存在: {target_dir}")

    group = settings.default_group if default_group is None else default_group.strip()
    lang_raw = settings.default_lang if default_lang is None else (default_lang.strip() or settings.default_lang)
    mkv_lang, track_name = build_track_name(group, lang_raw)

    mkvs = _iter_files(target_dir, settings.recursive, (".mkv",))
    if not mkvs:
        raise AssPipelineError(f"目录中未找到 MKV: {target_dir}")

    sub_files = _iter_files(target_dir, settings.recursive, (".ass", ".sup"))
    if not sub_files:
        raise AssPipelineError(f"目录中未找到 ASS/SUP 字幕: {target_dir}")

    by_parent: dict[Path, list[Path]] = {}
    for sub in sub_files:
        by_parent.setdefault(sub.parent.resolve(), []).append(sub)

    items: list[MuxPlanItem] = []
    total_sub_tracks = 0
    for mkv in mkvs:
        matched = find_subs_for_mkv(mkv, by_parent.get(mkv.parent.resolve(), []))
        if not matched:
            continue

        tracks: list[SubtitleTrackPlan] = []
        for sub in matched:
            rel_sub = sub.relative_to(target_dir)
            detected_lang_raw = infer_lang_raw_from_subtitle_name(sub.name, lang_raw)
            detected_mkv_lang, detected_track_name = build_track_name(group, detected_lang_raw)
            tracks.append(
                SubtitleTrackPlan(
                    file=str(rel_sub),
                    group=group,
                    lang_raw=detected_lang_raw,
                    mkv_lang=detected_mkv_lang,
                    track_name=detected_track_name,
                )
            )
            total_sub_tracks += 1

        items.append(MuxPlanItem(mkv=str(mkv.relative_to(target_dir)), subs=tracks))

    if not items:
        raise AssPipelineError("未找到任何可匹配字幕的 MKV（要求 MKV 与字幕位于同目录）")

    return MuxPlan(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        target_dir=str(target_dir),
        defaults={"group": group, "lang": lang_raw},
        items=items,
        total_mkvs=len(mkvs),
        matched_mkvs=len(items),
        total_sub_tracks=total_sub_tracks,
    )


def build_manual_mux_plan(settings: AssMuxSettings, *, default_group: str | None = None, default_lang: str | None = None) -> MuxPlan:
    target_dir = settings.target_dir.expanduser().resolve()
    if not target_dir.is_dir():
        raise AssPipelineError(f"ASS_MUX_TARGET_DIR 不存在: {target_dir}")

    group = settings.default_group if default_group is None else default_group.strip()
    lang_raw = settings.default_lang if default_lang is None else (default_lang.strip() or settings.default_lang)

    mkvs = sorted(path for path in target_dir.rglob("*") if path.is_file() and path.suffix.lower() == ".mkv")
    if not mkvs:
        raise AssPipelineError(f"目录中未找到 MKV: {target_dir}")

    items = [
        MuxPlanItem(
            mkv=str(mkv.relative_to(target_dir)),
            subs=[],
        )
        for mkv in mkvs
    ]

    return MuxPlan(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        target_dir=str(target_dir),
        defaults={"group": group, "lang": lang_raw},
        items=items,
        total_mkvs=len(mkvs),
        matched_mkvs=0,
        total_sub_tracks=0,
    )


def recount_mux_plan(plan: MuxPlan) -> MuxPlan:
    plan.matched_mkvs = sum(1 for item in plan.items if item.subs)
    plan.total_sub_tracks = sum(len(item.subs) for item in plan.items)
    return plan


def mux_plan_to_dict(plan: MuxPlan) -> dict:
    return asdict(plan)


def mux_plan_from_dict(data: dict) -> MuxPlan:
    items = [
        MuxPlanItem(
            mkv=str(item.get("mkv") or ""),
            subs=[SubtitleTrackPlan(**sub) for sub in item.get("subs") or []],
        )
        for item in data.get("items") or []
    ]
    return MuxPlan(
        generated_at=str(data.get("generated_at") or ""),
        target_dir=str(data.get("target_dir") or ""),
        defaults=dict(data.get("defaults") or {}),
        items=items,
        total_mkvs=int(data.get("total_mkvs") or 0),
        matched_mkvs=int(data.get("matched_mkvs") or 0),
        total_sub_tracks=int(data.get("total_sub_tracks") or 0),
    )


def write_mux_plan(plan: MuxPlan, path: Path) -> None:
    ensure_dir(path.expanduser().resolve().parent)
    path.write_text(json.dumps(mux_plan_to_dict(plan), ensure_ascii=False, indent=2), encoding="utf-8")


def format_mux_plan_preview(plan: MuxPlan, *, limit: int = 10) -> str:
    lines = [
        "🎞️ <b>/ass 字幕内封计划预览</b>",
        "",
        f"目录: <code>{html.escape(plan.target_dir)}</code>",
        f"默认字幕组: <code>{html.escape(plan.defaults.get('group', '') or '-')}</code>",
        f"默认语言: <code>{html.escape(plan.defaults.get('lang', '') or '-')}</code>",
        f"总 MKV: <code>{plan.total_mkvs}</code>",
        f"可处理视频: <code>{plan.matched_mkvs}</code>",
        f"字幕轨道数: <code>{plan.total_sub_tracks}</code>",
        "",
        "<b>预览：</b>",
    ]

    for index, item in enumerate(plan.items[:limit], 1):
        title = short_title_from_mkv(Path(item.mkv).name) or Path(item.mkv).stem
        ep = short_ep_display(Path(item.mkv).name)
        prefix = f"{index}. {ep} {title}".strip()
        lines.append(f"• <code>{html.escape(prefix)}</code>")
        lines.append(f"  ↳ <code>{html.escape(Path(item.mkv).name)}</code>")
        for sub in item.subs:
            lines.append(f"  ↳ 字幕: <code>{html.escape(Path(sub.file).name)}</code> / <code>{html.escape(sub.track_name)}</code> / <code>{html.escape(sub.mkv_lang)}</code>")

    remaining = len(plan.items) - limit
    if remaining > 0:
        lines.append(f"• ... 其余 <code>{remaining}</code> 项已省略")

    return "\n".join(lines)
