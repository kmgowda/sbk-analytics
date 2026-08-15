# Development Guide

Quick reference for developers and AI agents working on sbk-analytics.

## Quick Setup

```bash
# Clone and setup
git clone https://github.com/kmgowda/sbk-analytics.git
cd sbk-analytics

# Self-bootstrap and run on Linux/macOS (no activation required)
./sbk-analytics --version

# Or on Windows PowerShell
.\sbk-analytics.ps1 --version

# Create environment
conda env create -f environment.yml  # or use venv
conda activate sbk-analytics

# Install in development mode
pip install -e .

# Test installation
sbk-analytics --version
sbk-analytics -c examples/file-rocksdb-write-60s.yml
```

## Project Structure

```
analytics/              # Main package
├── cli.py             # CLI entry point
├── config.py          # YAML config parsing
├── releases.py        # Dependency resolution (JDK, SBK, sbk-charts)
├── runner.py          # SBK execution (serial/parallel)
├── charts.py          # sbk-charts invocation
├── processes.py       # managed process trees and signal cleanup
├── _process_guard.py  # POSIX parent-death companion
├── yaml_gen.py        # YAML generation for SBK
├── properties.py      # .env file parsing
└── system_info.py     # System info collection
```

## Key Files

- `sbk-config.env` - SBK versions, URLs, cache folders
- `sbk-analytics` - canonical application and platform dispatcher
- `sbk-analytics.sh` - Linux/macOS environment bootstrap and CLI launcher
- `sbk-analytics.ps1` - Windows environment bootstrap and CLI launcher
- `requirements.txt` - Python dependencies
- `pyproject.toml` - Package configuration
- `examples/` - Example configurations
- `AGENTS.md` - Comprehensive AI agent documentation

## Common Tasks

### Validate process cleanup
```bash
python -m unittest -v tests.test_process_cleanup
```

These integration tests terminate a controller normally and forcibly, then
verify that both its managed child and grandchild have stopped.

### Run a benchmark
```bash
sbk-analytics -c examples/file-rocksdb-write-60s.yml
```

### Run with verbose logging
```bash
sbk-analytics -c examples/config.yml -v
```

### Force log forwarding (macOS)
```bash
sbk-analytics -c examples/config.yml --forward-logs
```

### Test changes
```bash
# After making changes, reinstall
pip install -e .

# Test with example
sbk-analytics -c examples/config.yml
```

## Development Workflow

1. **Make changes** to code in `analytics/`
2. **Reinstall** with `pip install -e .`
3. **Test** with example configurations
4. **Update documentation** if needed
5. **Commit** with clear message

## Key Design Decisions

- **JDK Resolution**: SBK_JAVA_HOME (not JAVA_HOME) to avoid conflicts
- **Environment Detection**: Auto-detects conda vs venv
- **Caching**: External dependencies cached locally
- **macOS Handling**: Special subprocess handling for logging

## Dependencies

### Python
- PyYAML>=6.0
- requests>=2.28
- openpyxl>=3.1
- psutil>=5.9
- Pillow>=11.3 on Python 3.9, or Pillow>=12.0 on Python 3.10+ (for Excel images)
- openpyxl-image-loader>=1.0 (for image loading)

### External
- JDK (auto-downloaded)
- SBK (explicit local checkout or auto-downloaded)
- sbk-charts (explicit local checkout, conda package, or auto-installed)

## Debugging

### Enable verbose logging
```bash
sbk-analytics -c config.yml -v    # verbose
sbk-analytics -c config.yml -vv   # extra verbose
```

### Check cache locations
- JDK: `.jdk/<version>/` or `./.jdk/`
- SBK: `.sbk/<version>/`
- sbk-charts: `.sbk/sbk-charts/<version>/`

Local overrides use `sbk.local.folder` and `sbk-charts.local.folder` in
`sbk-config.env`. They are resolved before cache/network handling and must
already contain executable commands.

### Common issues
- **JDK version mismatch**: Check `sbk.jdk.version` in sbk-config.env
- **macOS logging**: Use `--forward-logs` flag
- **sbk-charts fails**: Check network and SSL settings

## Testing

### Test with different configs
```bash
# Serial mode
sbk-analytics -c examples/file-rocksdb-write-60s.yml

# Parallel mode
sbk-analytics -c examples/config.yml
```

### Test on different platforms
- Linux (primary)
- macOS (special logging handling)
- Windows (limited testing)

## Building Distribution

```bash
# Build
python -m build

# Test installation
pip install dist/sbk-analytics-*.tar.gz
sbk-analytics --version
```

## Documentation

- **AGENTS.md** - Comprehensive AI agent documentation (including YAML generation guide)
- **CONTRIBUTING.md** - Contribution guidelines
- **README.md** - User documentation
- **CHANGELOG.md** - Version history

### YAML Configuration Generation

For AI agents needing to generate YAML workload configurations, see the **YAML Configuration Generation for AI Agents** section in [AGENTS.md](AGENTS.md#yaml-configuration-generation-for-ai-agents), which includes:
- Complete YAML schema reference
- All storage driver classes and their parameters
- Common workload patterns with examples
- Best practices for YAML generation
- Validation rules and troubleshooting

## Environment Variables

- `SBK_JAVA_HOME` - JDK for SBK (set by sbk-analytics)
- `JAVA_HOME` - User's JAVA_HOME (not modified)
- `CONDA_PREFIX` - Conda environment detection
- `PYTHONUNBUFFERED` - Unbuffered Python output

## Getting Help

- See [AGENTS.md](AGENTS.md) for detailed documentation
- See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines
- Report issues on GitHub
