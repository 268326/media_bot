"""
STRM 空目录清理核心逻辑
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from typing import Sequence

from dotenv import dotenv_values

from strm_prune_emby import normalize_path, notify_after_delete

DEFAULT_ROOTS = (
    "/volume2/strm/share/电影",
    "/volume2/strm/share/电视剧",
    "/volume2/strm/share/动漫",
)


@dataclass(frozen=True)
class StrmPruneSettings:
    enabled: bool = False
    roots: tuple[str, ...] = DEFAULT_ROOTS
    allow_delete_first_level: bool = False
    include_roots: bool = False
    notify_emby: bool = False
    emby_url: str = "http://172.17.0.1:8096"
    emby_api_key: str = ""
    emby_update_type: str = "Deleted"
    http_timeout: int = 15
    http_retries: int = 3
    http_backoff: float = 2.0


@dataclass(frozen=True)
class RootInfo:
    path: str
    total_dirs: int


@dataclass(frozen=True)
class ScanResult:
    roots: list[RootInfo]
    total_dirs: int
    scanned_dirs: int
    deletable_dirs: list[str]
    errors: list[str]


@dataclass(frozen=True)
class ApplyResult:
    deleted_paths: list[str]
    parent_dirs: list[str]
    errors: list[str]
    emby_notified_dirs: list[str]
    emby_refreshed_item_ids: list[str]


@dataclass(frozen=True)
class RunResult:
    mode: str
    settings: StrmPruneSettings
    scan: ScanResult
    apply: ApplyResult | None


def _parse_bool(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on")


def _env_value(env_map: dict[str, str], key: str, default: str = "", *, fallback_os: bool | None = None) -> str:
    if key in env_map:
        return str(env_map.get(key, "") or "").strip()
    if fallback_os is None:
        fallback_os = not env_map
    if fallback_os:
        return str(os.getenv(key, default) or default).strip()
    return str(default or "").strip()


def _parse_roots(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return DEFAULT_ROOTS
    roots = [normalize_path(part.strip()) for part in raw.split("|") if part.strip()]
    return tuple(roots) or DEFAULT_ROOTS


def load_settings_from_env() -> StrmPruneSettings:
    dotenv_path = os.getenv("MEDIA_BOT_DOTENV_PATH", "/app/.env")
    env_map = {key: str(value) for key, value in dotenv_values(dotenv_path).items() if value is not None}

    emby_url = (
        _env_value(env_map, "STRM_PRUNE_EMBY_URL")
        or _env_value(env_map, "EMBY_URL")
        or "http://172.17.0.1:8096"
    )
    emby_api_key = (
        _env_value(env_map, "STRM_PRUNE_EMBY_API_KEY")
        or _env_value(env_map, "EMBY_API_KEY")
        or _env_value(env_map, "EMBYAPIKEY")
    )

    return StrmPruneSettings(
        enabled=_parse_bool(_env_value(env_map, "STRM_PRUNE_ENABLED", fallback_os=False), False),
        roots=_parse_roots(_env_value(env_map, "STRM_PRUNE_ROOTS", fallback_os=False)),
        allow_delete_first_level=_parse_bool(_env_value(env_map, "STRM_PRUNE_ALLOW_DELETE_FIRST_LEVEL", fallback_os=False), False),
        include_roots=_parse_bool(_env_value(env_map, "STRM_PRUNE_INCLUDE_ROOTS", fallback_os=False), False),
        notify_emby=_parse_bool(_env_value(env_map, "STRM_PRUNE_NOTIFY_EMBY", fallback_os=False), False),
        emby_url=emby_url,
        emby_api_key=emby_api_key,
        emby_update_type=_env_value(env_map, "STRM_PRUNE_EMBY_UPDATE_TYPE", "Deleted", fallback_os=False) or "Deleted",
        http_timeout=max(1, int(_env_value(env_map, "STRM_PRUNE_HTTP_TIMEOUT", "15", fallback_os=False))),
        http_retries=max(1, int(_env_value(env_map, "STRM_PRUNE_HTTP_RETRIES", "3", fallback_os=False))),
        http_backoff=max(1.0, float(_env_value(env_map, "STRM_PRUNE_HTTP_BACKOFF", "2.0", fallback_os=False))),
    )


def is_direct_child_of(root: str, path: str) -> bool:
    rel = os.path.relpath(path, root)
    if rel in (".", ""):
        return False
    return os.sep not in rel


def is_protected_first_level_child(path: str, protected_roots: Sequence[str]) -> bool:
    normalized_path = normalize_path(path)
    for protected_root in protected_roots:
        if is_direct_child_of(normalize_path(protected_root), normalized_path):
            return True
    return False


def path_depth(path: str) -> int:
    return normalize_path(path).count(os.sep)


def keep_topmost_dirs(paths: Sequence[str]) -> list[str]:
    uniq_paths = sorted({normalize_path(p) for p in paths}, key=lambda p: (path_depth(p), p))
    selected: list[str] = []
    for path in uniq_paths:
        if any(path == parent or path.startswith(parent + os.sep) for parent in selected):
            logging.info("↪️ 上级目录已可删除，跳过子目录：%s", path)
            continue
        selected.append(path)
    return selected


def unique_parent_dirs(paths: Sequence[str]) -> list[str]:
    parents = {normalize_path(os.path.dirname(path)) for path in paths}
    return sorted(parents, key=lambda p: (path_depth(p), p))


def count_directories(root: str, errors: list[str]) -> int:
    count = 0

    def onerror(exc: OSError):
        path = getattr(exc, "filename", None) or str(exc)
        msg = f"统计目录数量失败: {path}"
        errors.append(msg)
        logging.error("❌ %s", msg)

    for _, _, _ in os.walk(root, topdown=True, followlinks=False, onerror=onerror):
        count += 1
    return count


def prepare_roots(roots: Sequence[str]) -> tuple[list[RootInfo], list[str], int]:
    errors: list[str] = []
    root_infos: list[RootInfo] = []
    total_dirs = 0

    for root in roots:
        normalized_root = normalize_path(root)
        if not os.path.exists(normalized_root):
            msg = f"根目录不存在，已跳过: {normalized_root}"
            errors.append(msg)
            logging.warning("⚠️ %s", msg)
            continue
        if not os.path.isdir(normalized_root):
            msg = f"不是目录，已跳过: {normalized_root}"
            errors.append(msg)
            logging.warning("⚠️ %s", msg)
            continue

        root_total = count_directories(normalized_root, errors)
        total_dirs += root_total
        root_infos.append(RootInfo(path=normalized_root, total_dirs=root_total))
        logging.info("📊 预统计完成：%s | 目录总数 %s", normalized_root, root_total)

    return root_infos, errors, total_dirs


def collect_deletable_dirs(
    root_infos: Sequence[RootInfo],
    total_dirs_all: int,
    include_roots: bool,
    protect_first_level: bool,
) -> tuple[list[str], list[str], int]:
    subtree_has_strm: dict[str, bool] = {}
    deletable: list[str] = []
    errors: list[str] = []
    scanned_dirs = 0
    protected_roots = [info.path for info in root_infos]

    for info in root_infos:
        logging.info("🔍 开始扫描：%s", info.path)
        root_scanned = 0
        root_marked = 0

        def onerror(exc: OSError):
            path = getattr(exc, "filename", None) or str(exc)
            msg = f"读取失败: {path}"
            errors.append(msg)
            logging.error("❌ %s", msg)

        for dirpath, dirnames, filenames in os.walk(
            info.path,
            topdown=False,
            followlinks=False,
            onerror=onerror,
        ):
            normalized_dir = normalize_path(dirpath)
            scanned_dirs += 1
            root_scanned += 1

            root_pct = (root_scanned / info.total_dirs * 100) if info.total_dirs else 100.0
            all_pct = (scanned_dirs / total_dirs_all * 100) if total_dirs_all else 100.0
            logging.info(
                "🧭 扫描进度：根 %s/%s (%.1f%%) | 总 %s/%s (%.1f%%) | %s",
                root_scanned,
                info.total_dirs,
                root_pct,
                scanned_dirs,
                total_dirs_all,
                all_pct,
                normalized_dir,
            )

            has_own_strm = any(name.lower().endswith(".strm") for name in filenames)
            has_child_strm = any(
                subtree_has_strm.get(normalize_path(os.path.join(normalized_dir, dirname)), True)
                for dirname in dirnames
            )
            has_strm = has_own_strm or has_child_strm
            subtree_has_strm[normalized_dir] = has_strm

            if has_own_strm:
                logging.info("✅ 当前目录存在 .strm，保留：%s", normalized_dir)
                continue
            if has_child_strm:
                logging.info("✅ 子目录树存在 .strm，保留：%s", normalized_dir)
                continue
            if normalized_dir == info.path and not include_roots:
                logging.info("🛡️ 根目录为空但按配置不删除：%s", normalized_dir)
                continue
            if protect_first_level and is_protected_first_level_child(normalized_dir, protected_roots):
                logging.info("🛡️ 根目录下一级子目录按规则保留：%s", normalized_dir)
                continue

            deletable.append(normalized_dir)
            root_marked += 1
            logging.info("🗑️ 标记待删除：%s", normalized_dir)

        logging.info("✅ 根目录扫描完成：%s | 扫描 %s | 待删除 %s", info.path, root_scanned, root_marked)

    return deletable, errors, scanned_dirs


def scan(settings: StrmPruneSettings) -> ScanResult:
    root_infos, prepare_errors, total_dirs_all = prepare_roots(settings.roots)
    if not root_infos:
        return ScanResult(
            roots=[],
            total_dirs=0,
            scanned_dirs=0,
            deletable_dirs=[],
            errors=prepare_errors,
        )

    deletable, scan_errors, scanned_dirs = collect_deletable_dirs(
        root_infos,
        total_dirs_all,
        include_roots=settings.include_roots,
        protect_first_level=not settings.allow_delete_first_level,
    )
    return ScanResult(
        roots=list(root_infos),
        total_dirs=total_dirs_all,
        scanned_dirs=scanned_dirs,
        deletable_dirs=keep_topmost_dirs(deletable),
        errors=prepare_errors + scan_errors,
    )


def apply(scan_result: ScanResult, settings: StrmPruneSettings) -> ApplyResult:
    deleted_paths: list[str] = []
    errors = list(scan_result.errors)
    failed: list[str] = []

    for path in sorted(scan_result.deletable_dirs, key=lambda p: (path_depth(p), p), reverse=True):
        logging.info("🚧 正在删除：%s", path)
        try:
            shutil.rmtree(path)
            deleted_paths.append(path)
            logging.info("✅ 已删除：%s", path)
        except Exception as exc:
            msg = f"删除失败: {path} -> {exc}"
            failed.append(msg)
            logging.exception("❌ %s", msg)

    errors.extend(failed)
    parent_dirs = unique_parent_dirs(deleted_paths)
    emby_notified_dirs: list[str] = []
    emby_refreshed_item_ids: list[str] = []

    if settings.notify_emby and deleted_paths:
        if not settings.emby_api_key:
            errors.append("未配置 STRM_PRUNE_EMBY_API_KEY / EMBY_API_KEY，已跳过 Emby 通知")
        else:
            notify_result = notify_after_delete(
                parent_dirs=parent_dirs,
                emby_url=settings.emby_url,
                api_key=settings.emby_api_key,
                update_type=settings.emby_update_type,
                timeout=settings.http_timeout,
                retries=settings.http_retries,
                backoff=settings.http_backoff,
            )
            emby_notified_dirs = list(notify_result.get("notified_dirs", []))
            emby_refreshed_item_ids = list(notify_result.get("refreshed_item_ids", []))
            errors.extend(list(notify_result.get("errors", [])))

    return ApplyResult(
        deleted_paths=deleted_paths,
        parent_dirs=parent_dirs,
        errors=errors,
        emby_notified_dirs=emby_notified_dirs,
        emby_refreshed_item_ids=emby_refreshed_item_ids,
    )


def run_prune(settings: StrmPruneSettings, apply_changes: bool) -> RunResult:
    mode = "apply" if apply_changes else "dry-run"
    logging.info(
        "🧹 STRM 空目录清理开始 | mode=%s | roots=%s | allow_delete_first_level=%s | include_roots=%s | notify_emby=%s",
        mode,
        list(settings.roots),
        settings.allow_delete_first_level,
        settings.include_roots,
        settings.notify_emby,
    )
    scan_result = scan(settings)
    if not apply_changes:
        return RunResult(mode=mode, settings=settings, scan=scan_result, apply=None)
    apply_result = apply(scan_result, settings)
    return RunResult(mode=mode, settings=settings, scan=scan_result, apply=apply_result)
