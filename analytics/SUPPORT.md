# Support and Help

This document provides guidance on how to get help with sbk-analytics.

## Getting Started

If you're new to sbk-analytics, start with:
- **README.md** - Installation and basic usage
- **examples/** - Example configuration files
- **AGENTS.md** - Technical documentation for AI agents

## Common Issues

### Installation Problems

**Problem**: `pip install -e .` fails
- Ensure you're using Python 3.9+
- Try using conda: `conda env create -f environment.yml`
- Check that dependencies are available in your environment

**Problem**: sbk-charts installation fails
- Check network connectivity
- Verify SSL settings in `sbk-config.env`
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
- Check YAML configuration syntax
- Verify file paths exist
- Use verbose logging: `sbk-analytics -c config.yml -v`
- Check SBK logs in workdir/logs/ (parallel mode)

**Problem**: a workload appears to remain after sbk-analytics is stopped
- Current releases terminate the complete local process tree for `sbk-yal`,
  `sbk-gem-yal`, and `sbk-charts`; verify with `ps`/Task Manager that the
  process is from the same invocation
- Allow up to 3 seconds for graceful shutdown before forced termination
- Use `-v` to see termination messages for catchable signals
- Remote SBK clients receive best-effort SSH cleanup on catchable interrupts;
  after an uncatchable local kill, inspect the remote nodes separately

**Problem**: `cleanup: on-success` did not remove benchmark data
- Cleanup intentionally supports only `class: file` and its `file`/`fname`
  parameter
- The resolved data path must be strictly inside `workdir`
- RocksDB, other drivers, external paths, CSVs, logs, and reports are preserved

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
- Python version: `python --version`
- sbk-analytics version: `sbk-analytics --version`
- Configuration files (sanitized)
- Error messages and logs
- Steps to reproduce

## Known Limitations

- **JDK Compatibility**: SBK compiled with specific Java versions
- **macOS Logging**: Requires special handling with `--forward-logs`
- **Windows Support**: Limited testing on Windows
- **Network Dependencies**: Requires internet for initial downloads

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
