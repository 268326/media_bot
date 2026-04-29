from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

FONT_SUFFIXES = {'.ttf', '.ttc', '.otf', '.otc'}
ARCHIVE_SUFFIXES = {'.7z', '.zip'}
GENERATED_ASS_SUFFIX = '.assfonts.ass'


class AssPipelineError(RuntimeError):
    pass


@dataclass(slots=True)
class ScanResult:
    ass_files: list[Path]
    archives: list[Path]
    font_dirs: list[Path]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def reset_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        resolved = str(path.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        if path.exists() and not path.is_dir():
            continue
        seen.add(resolved)
        result.append(path)
    return result


def iter_files(root: Path, recursive: bool, exclude_dirs: list[Path] | None = None):
    excludes = [p.resolve() for p in (exclude_dirs or []) if p.exists()]
    iterator = root.rglob('*') if recursive else root.glob('*')
    for path in iterator:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if any(_is_under(resolved, ex) for ex in excludes):
            continue
        if path.is_file():
            yield path


def scan_root(root: Path, recursive: bool, exclude_dirs: list[Path] | None = None) -> ScanResult:
    ass_files: list[Path] = []
    archives: list[Path] = []
    font_dirs: set[Path] = set()
    for path in iter_files(root, recursive, exclude_dirs=exclude_dirs):
        name = path.name.lower()
        suffix = path.suffix.lower()
        if name.endswith('.ass') and not name.endswith(GENERATED_ASS_SUFFIX):
            ass_files.append(path)
        elif suffix in ARCHIVE_SUFFIXES:
            archives.append(path)
        elif suffix in FONT_SUFFIXES:
            font_dirs.add(path.parent.resolve())
    ass_files.sort()
    archives.sort()
    return ScanResult(ass_files=ass_files, archives=archives, font_dirs=sorted(font_dirs))


def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    logger.info('▶ %s', ' '.join(cmd))
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, capture_output=True)
    if result.stdout:
        for line in result.stdout.splitlines():
            logger.info('%s', line)
    if result.stderr:
        for line in result.stderr.splitlines():
            logger.warning('%s', line)
    if result.returncode != 0:
        joined = ' '.join(cmd)
        raise AssPipelineError(f'Command failed ({result.returncode}): {joined}')
    return result


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
