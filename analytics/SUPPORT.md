# Support and Help

This document provides guidance on how to get help with sbk-analytics.

## Getting Started

If you're new to sbk-analytics, start with:
- **README.md** - Installation and basic usage
- **examples/** - Example configuration files
- **AGENTS.md** - Technical documentation for AI agents

## Common Issues

### Installation Problems

**Problem**: a self-bootstrapping launcher cannot prepare an environment
- Use `./sbk-analytics` on Linux or macOS. Native Windows is not supported.
- No host Python, venv, or Conda is required. The first uv download needs
  `curl` or `wget`.
- Confirm GitHub and Python package repositories are reachable on first use
- Confirm the per-user state directory, or `SBK_ANALYTICS_ENV_HOME`, is writable
- Set `SBK_ANALYTICS_BOOTSTRAP_OFFLINE=1` only after the runtime is cached; a
  healthy saved environment is automatically reused without invoking uv
- A checksum error is never bypassed. Check proxy/content rewriting and the
  platform SHA-256 values in `sbk-bootstrap.env`
- Runtime source, root/package configuration, example YAML, launcher, or lock
  changes create a freshly built versioned environment. Corrupt or interrupted
  staging directories are repaired without reusing partial or stale-wheel state

**Problem**: `pip install -e .` fails
- Ensure you're using Python 3.9+
- Try using conda: `conda env create -f environment.yml`
- Check that dependencies are available in your environment

**Problem**: sbk-charts installation fails
- Check network connectivity
- Verify SSL settings in `sbk-config.env`
- Verify `sbk-charts.sha256` matches the configured version's tag archive
- Try with `ssl.verify=false` in sbk-config.env

**Problem**: a configured local SBK or sbk-charts folder is rejected
- Confirm the local package is already built and runnable
- SBK must contain `bin/sbk-yal`, either directly or under
  `build/install/sbk/`; GEM workloads also require `bin/sbk-gem-yal`
- sbk-charts must contain `sbk-charts` at its root or `bin/sbk-charts`
- Ensure the commands are executable; invalid explicit local folders never
  fall back to downloaded packages
- Run `sbk-analytics deps doctor -p sbk-config.env -vv` for executable,
  version, JDK, and sbk-charts startup checks
- Use `sbk-analytics deps status -p sbk-config.env --json` for a read-only
  view that never downloads or modifies dependencies
- If a checkout layout is unusual, configure the exact command with
  `sbk-charts.local.executable` or `--sbk-charts-executable`
- sbk-analytics never runs an SBK build. After changing SBK source, rebuild it
  with the SBK project's own development workflow before running analytics.
- `deps status --json` reports shared-folder layout, resolved paths, Git
  revision/tracked-file dirty state, and cached release metadata without
  starting anything. Run with `-vv` to see Git inspection failures or timeouts.

### Runtime Issues

**Problem**: JDK version mismatch (`UnsupportedClassVersionError`)
- Check `sbk.jdk.version` in `sbk-config.env`
- Ensure SBK_JAVA_HOME is not pointing to wrong version
- Let sbk-analytics auto-download the correct JDK

**Problem**: macOS logs not visible
- Use `--forward-logs` flag: `sbk-analytics -c config.yml --forward-logs`
- Check terminal buffering settings
- Ensure dependencies are installed

**Problem**: SBK instances fail
- Confirm the selected SBK implements the shipped baseline command contract
- Check YAML configuration syntax
- Verify file paths exist
- Use verbose logging: `sbk-analytics -c config.yml -v`
- Check SBK logs in workdir/logs/ (parallel mode)

**Problem**: a workload appears to remain after sbk-analytics is stopped
- Current releases terminate the complete local process tree for `sbk-yal`,
  `sbk-gem-yal`, and `sbk-charts`
- Allow up to 30 seconds for SBK-GEM to perform its native remote cleanup
- Use `-v` to see termination messages for catchable signals
- Run `sbk-analytics deps status` to inspect active, stale, or quarantined
  ownership records without changing them
- Run `sbk-analytics deps doctor` or the next benchmark to reconcile a verified
  stale local process group. Records whose PID/start-time/command identity does
  not match are quarantined and never signalled
- Remote SBK clients and embedded SBM are owned by SBK-GEM. Analytics does not
  run global remote `pkill` commands because they can stop unrelated workloads
- Set `SBK_ANALYTICS_LIFECYCLE_FOLDER` only when the default per-user state
  directory is unsuitable; the folder contains no GEM credentials
- The lifecycle registry is a required safety dependency. A full filesystem,
  an unavailable mount, or an unwritable registry directory aborts workload
  startup so SBK or sbk-charts never runs without durable ownership tracking.
  Restore free space and write access, or point
  `SBK_ANALYTICS_LIFECYCLE_FOLDER` to a private writable directory
- Unsupported lifecycle-record schemas are quarantined as `.unresolved`
  rather than migrated or used to signal a possibly unrelated process. Use
  `-vv` to distinguish restricted process-environment inspection from an
  actual ownership mismatch

**Problem**: `cleanup: on-success` did not remove benchmark data
- Cleanup intentionally supports only `class: file` and its `file`/`fname`
  parameter
- The resolved data path must be strictly inside `workdir`
- RocksDB, other drivers, external paths, CSVs, logs, and reports are preserved

**Problem**: `cleanup_before_run: true` was refused
- This option deletes every entry below `workdir`, so it refuses filesystem,
  home, current/source, system-temp, configuration, dependency, JDK, and cache
  scopes that could remove application or shared data
- Use a dedicated benchmark directory such as `/tmp/sbk-analytics`, and keep
  reusable `sbk-charts.use_files` outside it
- A deletion error aborts the run; correct ownership, mount, or permissions
  before retrying

### Output Issues

**Problem**: Excel report not generated
- Ensure at least one SBK instance succeeded
- Check sbk-charts installation
- Verify workdir permissions
- Check sbk-charts logs

**Problem**: SBK logo missing in Excel
- Ensure Pillow>=11.3 on Python 3.9, or Pillow>=12.0 on Python 3.10+
- Ensure openpyxl-image-loader>=1.0 is installed
- Reinstall dependencies: `pip install -r requirements.txt`

## Getting Help

### Documentation
- **README.md** - User documentation
- **AGENTS.md** - Comprehensive technical documentation
- **CONTRIBUTING.md** - Contribution guidelines
- **DEVELOPMENT.md** - Development quick reference

### Community
- **GitHub Issues** - Report bugs and feature requests
- **GitHub Discussions** - Ask questions and share ideas

### Debug Mode

Enable verbose logging for troubleshooting:
```bash
# Verbose
sbk-analytics -c config.yml -v

# Extra verbose
sbk-analytics -c config.yml -vv
```

### Log Locations

- **SBK logs**: `<workdir>/logs/` (parallel mode)
- **sbk-charts logs**: Console output
- **System logs**: Console output with verbose mode

### Environment Information

When reporting issues, include:
- Operating system and version
- Managed runtime metadata from `<runtime-state>/app/<fingerprint>/metadata.json`
- sbk-analytics version: `sbk-analytics --version`
- Configuration files (sanitized)
- Error messages and logs
- Steps to reproduce

## Known Limitations

- **JDK Compatibility**: SBK compiled with specific Java versions
- **macOS Logging**: Requires special handling with `--forward-logs`
- **Native Windows**: Not supported; use a Linux or macOS host
- **Network Dependencies**: Requires internet for uncached first-run artifacts;
  healthy saved environments and populated dependency caches run offline

## Performance Tips

- Use parallel mode for multiple independent benchmarks
- Cache dependencies to avoid re-downloading
- Use appropriate workdir for faster I/O
- Monitor system resources during execution

## AI Agent Support

For AI coding assistants, see:
- **AGENTS.md** - Comprehensive project documentation
- **CONTRIBUTING.md** - Development guidelines
- **DEVELOPMENT.md** - Quick development reference

## Contact

- **GitHub**: https://github.com/kmgowda/sbk-analytics
- **Issues**: https://github.com/kmgowda/sbk-analytics/issues
- **Discussions**: https://github.com/kmgowda/sbk-analytics/discussions

## License

This project is licensed under the Apache-2.0 License.
