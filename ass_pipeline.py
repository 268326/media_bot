from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ass_config import AssSettings
from ass_font_pool import FontPoolBuilder
from ass_utils import AssPipelineError, ensure_dir, reset_dir, run_cmd, scan_root, unique_paths

logger = logging.getLogger(__name__)
SYSTEM_FONT_DIRS = [Path('/usr/share/fonts'), Path('/usr/local/share/fonts')]


@dataclass(slots=True)
class AssRunSummary:
    target_dir: str
    total_ass: int
    processed: int
    skipped: int
    failed: int
    archives: int
    font_dirs: int
    converted_otf: int
    skipped_otf: int
    duration_s: float
    outputs: list[str]
    failures: list[str]


def run_ass_pipeline(settings: AssSettings) -> AssRunSummary:
    started = time.monotonic()
    target_dir = settings.target_dir.expanduser().resolve()
    if not target_dir.is_dir():
        raise AssPipelineError(f'ASS_TARGET_DIR 不存在: {target_dir}')

    work_dir = settings.work_dir.expanduser().resolve()
    try:
        target_dir.relative_to(work_dir)
        raise AssPipelineError(f'ASS_WORK_DIR 不能等于或位于 ASS_TARGET_DIR 的上层目录: work={work_dir} target={target_dir}')
    except ValueError:
        pass

    reset_dir(work_dir)
    scan = scan_root(target_dir, settings.recursive, exclude_dirs=[work_dir])
    if not scan.ass_files:
        raise AssPipelineError(f'目录中未找到 ASS: {target_dir}')

    pending = []
    outputs = []
    for ass_file in scan.ass_files:
        output_path = ass_file.with_name(f'{ass_file.stem}.assfonts{ass_file.suffix}')
        if output_path.exists():
            logger.info('⏭️ 跳过已存在成品: %s', output_path)
            outputs.append(str(output_path))
            continue
        pending.append(ass_file)

    if not pending:
        duration = time.monotonic() - started
        return AssRunSummary(str(target_dir), len(scan.ass_files), 0, len(scan.ass_files), 0, len(scan.archives), 0, 0, 0, duration, outputs, [])

    extract_root = ensure_dir(work_dir / 'extract')
    extracted_font_dirs: list[Path] = []
    for archive in scan.archives:
        out_dir = extract_root / archive.stem
        reset_dir(out_dir)
        if archive.suffix.lower() == '.7z':
            run_cmd([settings.sevenz_bin, 'x', '-y', f'-o{out_dir}', str(archive)])
        else:
            run_cmd([settings.unzip_bin, '-o', str(archive), '-d', str(out_dir)])
        extracted_font_dirs.extend(scan_root(out_dir, True).font_dirs)

    font_dirs = list(scan.font_dirs) + extracted_font_dirs
    if settings.include_system_fonts:
        font_dirs.extend(path for path in SYSTEM_FONT_DIRS if path.is_dir())
    font_dirs = unique_paths(font_dirs)
    if not font_dirs:
        raise AssPipelineError('未找到任何字体目录')

    pool_builder = FontPoolBuilder(work_dir / 'prepared_fonts', fontforge_bin=settings.fontforge_bin)
    prepared_root, converted_otf, skipped_otf = pool_builder.build(font_dirs, exclude_dirs=[work_dir])
    prepared_dirs = [path for path in prepared_root.iterdir() if path.is_dir()]
    if not prepared_dirs:
        raise AssPipelineError('纯 TTF 字体池为空，无法继续')

    db_dir = ensure_dir(work_dir / 'db')
    build_db_cmd = [settings.assfonts_bin, '-v', '3', '-d', str(db_dir)]
    for font_dir in prepared_dirs:
        build_db_cmd.extend(['-f', str(font_dir)])
    build_db_cmd.append('-b')
    run_cmd(build_db_cmd)

    processed = 0
    failures: list[str] = []
    for ass_file in pending:
        try:
            logger.info('🎞️ 处理 ASS: %s', ass_file)
            rel_name = '__'.join(ass_file.relative_to(target_dir).parts)
            job_name = rel_name.replace('/', '_').replace('\\', '_')
            job_dir = reset_dir(work_dir / 'jobs' / job_name)
            subset_output = ensure_dir(job_dir / 'subset_output')
            embed_output = ensure_dir(job_dir / 'embed_output')
            run_cmd([settings.assfonts_bin, '-v', '3', '-d', str(db_dir), '-o', str(subset_output), '-s', '-i', str(ass_file)])
            subset_dir = subset_output / f'{ass_file.stem}_subsetted'
            if not subset_dir.is_dir():
                raise AssPipelineError(f'subset 结果目录不存在: {subset_dir}')
            run_cmd([settings.assfonts_bin, '-v', '3', '-d', str(db_dir), '-o', str(embed_output), '-f', str(subset_dir), '-e', '-i', str(ass_file)])
            generated = embed_output / f'{ass_file.stem}.assfonts{ass_file.suffix}'
            if not generated.is_file():
                raise AssPipelineError(f'内嵌结果不存在: {generated}')
            final_output = ass_file.with_name(generated.name)
            final_output.write_bytes(generated.read_bytes())
            outputs.append(str(final_output))
            processed += 1
        except Exception as exc:
            logger.exception('❌ 处理 ASS 失败: %s', ass_file)
            failures.append(f'{ass_file.name}: {exc}')

    failed = len(failures)
    skipped = len(scan.ass_files) - len(pending)
    duration = time.monotonic() - started
    return AssRunSummary(
        target_dir=str(target_dir),
        total_ass=len(scan.ass_files),
        processed=processed,
        skipped=skipped,
        failed=failed,
        archives=len(scan.archives),
        font_dirs=len(font_dirs),
        converted_otf=converted_otf,
        skipped_otf=skipped_otf,
        duration_s=duration,
        outputs=outputs,
        failures=failures,
    )
