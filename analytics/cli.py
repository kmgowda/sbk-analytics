"""sbk-analytics command-line entry point."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from .charts import run_sbk_charts
from .config import load_config
from .properties import parse_properties
from .releases import ensure_sbk, ensure_sbk_charts
from .runner import run_jobs
from .system_info import append_system_sheet
from .yaml_gen import generate_instance_yaml

log = logging.getLogger("sbk-analytics")


def _bundled_versions_file() -> Path:
    """Return the project-bundled `versions.env` shipped next to this package.

    The file lives at the repository root (`<project>/versions.env`).
    """
    return Path(__file__).resolve().parent.parent / "versions.env"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sbk-analytics",
        description=(
            "Orchestrate multiple SBK (sbk-yal / sbk-gem-yal) benchmark runs and "
            "feed the resulting CSVs into sbk-charts for combined Excel + AI analytics."
        ),
    )
    p.add_argument(
        "-p",
        "--properties",
        type=Path,
        default=None,
        help=(
            "Versions properties file (key=value): sbk.version, "
            "sbk-charts.version. Defaults to the bundled "
            "<project>/versions.env shipped with sbk-analytics."
        ),
    )
    p.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Input YML with the 8 parameter groups (mode, sbk, classes, ...)",
    )
    p.add_argument(
        "-w",
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Working directory for generated YAMLs, CSV files, per-class logs "
            "and the final Excel report. Precedence: this flag > the input "
            "YAML's `workdir:` key > /tmp/sbk-analytics."
        ),
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase log verbosity."
    )
    return p.parse_args(argv)


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING - 10 * verbosity
    level = max(logging.DEBUG, level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    properties_path = args.properties or _bundled_versions_file()
    if not properties_path.is_file():
        raise SystemExit(
            f"versions properties file not found: {properties_path}\n"
            f"either pass -p / --properties or restore the bundled "
            f"{_bundled_versions_file()}"
        )
    versions = parse_properties(properties_path)
    log.info("using properties file: %s", properties_path)
    cfg = load_config(args.config)

    log.info("SBK: %s @ %s", versions.sbk_url, versions.sbk)
    log.info("sbk-charts: %s @ %s", versions.sbk_charts_url, versions.sbk_charts)
    log.info(
        "mode=%s instances=%d uses_gem=%s",
        cfg.mode,
        len(cfg.instances),
        cfg.uses_gem,
    )
    for inst in cfg.instances:
        log.info("  - %s (class=%s) params=%s", inst.name, inst.class_name, inst.params)

    # Working directory: precedence is
    #   1. -w / --work-dir CLI flag
    #   2. workdir: in the input YAML (just after `mode:`)
    #   3. /tmp/sbk-analytics  (DEFAULT_WORKDIR)
    work = args.work_dir if args.work_dir is not None else Path(cfg.workdir)
    work.mkdir(parents=True, exist_ok=True)
    log.info("work dir: %s", work.resolve())

    # 1. Fetch SBK and sbk-charts
    sbk = ensure_sbk(versions.sbk, repo=versions.sbk_repo)
    charts = ensure_sbk_charts(versions.sbk_charts, repo_url=versions.sbk_charts_url)

    executable = sbk.sbk_gem_yal if cfg.uses_gem else sbk.sbk_yal
    log.info("using SBK executable: %s", executable)

    # 2. Generate per-class YAMLs
    yml_dir = work / "yml"
    csv_dir = work / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, Path, Path]] = []
    for inst in cfg.instances:
        csv_path = (csv_dir / f"sbk-{inst.name}.csv").resolve()
        yml_path = generate_instance_yaml(inst, yml_dir, csv_path)
        jobs.append((inst.name, yml_path, csv_path))

    # 3. Run SBK instances
    log_dir = work / "logs"
    results = run_jobs(executable, jobs, mode=cfg.mode, log_dir=log_dir)

    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print("\n=== SBK run summary ===", flush=True)
    for r in results:
        status = "OK" if r.ok else f"FAIL(rc={r.returncode})"
        extra = f" log={r.log_path}" if r.log_path else ""
        print(f"  {status:14s} instance={r.class_name} csv={r.csv_path}{extra}")

    # Resolve sbk-charts.use_files: take each entry as a CSV path, optionally
    # relative to the working directory. Drop (with a warning) any that don't
    # exist; sbk-charts would fail anyway.
    extra_csvs: list[Path] = []
    for raw_path in cfg.use_files:
        p = Path(raw_path)
        if not p.is_absolute():
            p = (work / p).resolve()
        else:
            p = p.resolve()
        if p.is_file() and p.stat().st_size > 0:
            extra_csvs.append(p)
        else:
            print(
                f"WARNING: sbk-charts.use_files entry ignored (missing or "
                f"empty): {p}",
                file=sys.stderr, flush=True,
            )
    if extra_csvs:
        print("\n=== sbk-charts use_files (pre-existing) ===", flush=True)
        for p in extra_csvs:
            print(f"  USE            csv={p}")

    if not succeeded and not extra_csvs:
        print(
            "All SBK instances failed and no use_files supplied; skipping "
            "sbk-charts as per spec.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    if failed:
        print(
            f"WARNING: {len(failed)} of {len(results)} SBK runs failed; "
            f"continuing with {len(succeeded)} fresh CSV(s) and "
            f"{len(extra_csvs)} pre-existing use_files.",
            file=sys.stderr,
            flush=True,
        )

    # 4. sbk-charts (once)
    # The output xlsx lives in <workdir> unless cfg.output is an absolute path
    # (or contains an explicit directory component) -- in which case we honour
    # the user's exact location.
    output_p = Path(cfg.output)
    if output_p.is_absolute() or len(output_p.parts) > 1:
        output_xlsx = output_p.resolve()
    else:
        output_xlsx = (work / output_p).resolve()
    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    csv_paths = [r.csv_path for r in succeeded] + extra_csvs

    rc = run_sbk_charts(charts, cfg, csv_paths, output_xlsx, work_dir=work)
    if rc != 0:
        log.error("sbk-charts exited with rc=%s", rc)
        return rc
    if not output_xlsx.exists():
        log.error("sbk-charts did not produce expected output: %s", output_xlsx)
        return 3

    # 5. Append system sheet
    try:
        append_system_sheet(output_xlsx)
    except Exception as e:
        log.error("failed to append system sheet: %s", e)
        return 4

    print(f"\nDone. Output: {output_xlsx}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
