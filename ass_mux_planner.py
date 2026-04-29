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
            tracks.append(
                SubtitleTrackPlan(
                    file=str(rel_sub),
                    group=group,
                    lang_raw=lang_raw,
                    mkv_lang=mkv_lang,
                    track_name=track_name,
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
