from __future__ import annotations

import copy
import logging
import shutil
from pathlib import Path

from fontTools.ttLib import TTFont

from ass_utils import AssPipelineError, ensure_dir, reset_dir, run_cmd

logger = logging.getLogger(__name__)


class FontPoolBuilder:
    def __init__(self, prepared_root: Path, *, fontforge_bin: str) -> None:
        self.prepared_root = prepared_root
        self.fontforge_bin = fontforge_bin

    def build(self, font_dirs: list[Path], *, exclude_dirs: list[Path] | None = None) -> tuple[Path, int, int]:
        reset_dir(self.prepared_root)
        excludes = [p.resolve() for p in (exclude_dirs or []) if p.exists()]
        converted = 0
        skipped = 0
        for index, font_dir in enumerate(font_dirs, start=1):
            target_root = self.prepared_root / f'{index:03d}_{self._safe_name(font_dir)}'
            c_count, s_count = self._copy_one_dir(font_dir.resolve(), target_root, excludes)
            converted += c_count
            skipped += s_count
        return self.prepared_root, converted, skipped

    def _copy_one_dir(self, source_dir: Path, target_root: Path, excludes: list[Path]) -> tuple[int, int]:
        converted = 0
        skipped = 0
        logger.info('🧰 准备字体目录: %s', source_dir)
        active_excludes = [ex for ex in excludes if self._is_under(ex, source_dir)]
        for path in sorted(source_dir.rglob('*')):
            resolved = path.resolve()
            if any(self._is_under(resolved, ex) for ex in active_excludes):
                continue
            rel = path.relative_to(source_dir)
            target = target_root / rel
            if path.is_dir():
                ensure_dir(target)
                continue
            suffix = path.suffix.lower()
            if suffix in {'.ttf', '.ttc'}:
                ensure_dir(target.parent)
                shutil.copy2(path, target)
                continue
            if suffix == '.otf':
                ensure_dir(target.parent)
                ttf_target = self._unique_ttf_target(target.with_suffix('.ttf'))
                try:
                    self._convert_otf_to_ttf(path, ttf_target)
                    converted += 1
                except AssPipelineError as exc:
                    skipped += 1
                    logger.warning('⚠️ 跳过无法转换的 OTF: %s (%s)', path, exc)
                continue
            if suffix == '.otc':
                skipped += 1
                logger.warning('⚠️ 跳过暂不支持的 OTC: %s', path)
        return converted, skipped

    def _convert_otf_to_ttf(self, source_otf: Path, target_ttf: Path) -> None:
        run_cmd([
            self.fontforge_bin,
            '-lang=py',
            '-c',
            "import fontforge; "
            f"font=fontforge.open({str(source_otf)!r}); "
            f"font.generate({str(target_ttf)!r}); "
            "font.close()",
        ])
        if not target_ttf.exists():
            raise AssPipelineError(f'OTF 转 TTF 失败: {source_otf}')
        self._copy_name_table(source_otf, target_ttf)

    def _copy_name_table(self, source_otf: Path, target_ttf: Path) -> None:
        src = TTFont(str(source_otf))
        dst = TTFont(str(target_ttf))
        if 'name' not in src or 'name' not in dst:
            raise AssPipelineError(f'name table 缺失: {source_otf}')
        dst['name'].names = [copy.deepcopy(record) for record in src['name'].names]
        dst.save(str(target_ttf))

    @staticmethod
    def _safe_name(path: Path) -> str:
        return (str(path).strip('/').replace('/', '_').replace('\\', '_') or 'fontdir')

    @staticmethod
    def _unique_ttf_target(target: Path) -> Path:
        if not target.exists():
            return target
        index = 1
        while True:
            candidate = target.with_name(f'{target.stem}.from_otf_{index}{target.suffix}')
            if not candidate.exists():
                return candidate
            index += 1

    @staticmethod
    def _is_under(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False
