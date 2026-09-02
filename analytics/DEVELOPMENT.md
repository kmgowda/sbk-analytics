# Development Guide

Quick reference for developers and AI agents working on sbk-analytics.

## Quick Setup

```bash
# Clone and setup
git clone https://github.com/kmgowda/sbk-analytics.git
cd sbk-analytics

# Self-bootstrap and run on Linux/macOS (no activation required)
./sbk-analytics --version

# Optional manual editable environment for development only
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# Test installation
sbk-analytics --version
sbk-analytics -c examples/file-rocksdb-write-60s.yml

# Fast shared-folder SBK 10.6+ and sbk-charts validation
sbk-analytics --sbk-local /path/to/SBK \
  --sbk-charts-local /path/to/sbk-charts \
  -c examples/local-rocksdb-smoke-test.yml
```

## Project Structure

```
analytics/              # Main package
├── cli.py             # CLI entry point
├── workflow.py        # ordered benchmark/report execution pipeline
├── policy.py          # runtime policy and managed-artifact metadata
├── config.py          # YAML config parsing
├── sbk_contract.py    # supported SBK option contract and migrations
├── releases/          # Dependency resolution package
│   ├── _shared.py     # cache/download/archive/provenance primitives
│   ├── sbk.py         # SBK resolver
│   ├── charts.py      # sbk-charts resolver
│   └── jdk.py         # JDK resolver
├── runner.py          # SBK execution (serial/parallel)
├── charts.py          # sbk-charts invocation
├── processes.py       # managed process trees and signal cleanup
├── lifecycle.py       # durable ownership and stale-run reconciliation
├── _process_guard.py  # POSIX parent-death companion
├── yaml_gen.py        # YAML generation for SBK
├── properties.py      # .env file parsing
└── system_info.py     # System info collection
```

## Key Files

- `sbk-config.env` - SBK versions, URLs, cache folders
- `sbk-bootstrap.env` - Linux/macOS Bash bootstrap policy
- `.python-version` - exact launcher-managed Python version
- `uv.lock` - reproducible cross-platform application dependency lock
- `sbk-analytics` - canonical Linux/macOS application
- `sbk-analytics.sh` - Linux/macOS environment bootstrap and CLI launcher
- `requirements.txt` - Python dependencies
- `pyproject.toml` - Package configuration
- `examples/` - Example configurations
- `AGENTS.md` - Comprehensive AI agent documentation

## Runtime policy changes

Change cross-cutting operational defaults in `analytics/policy.py`. Its frozen
dataclasses own dependency identities and repository defaults, managed-cache
layout, source/provenance vocabulary, executable and environment names, command
interfaces, network/retry settings, display units, process and benchmark
timing, SSH behavior, configuration defaults, and exit codes. Keep release
version pins and operator selections in `sbk-config.env`; named constants used
by only one algorithm remain next to that algorithm. Do not duplicate a policy
literal in a consumer. Add or update `tests/test_policy.py` whenever policy
metadata or an ordering constraint changes. Pre-Python values shared by the
native launchers belong in `sbk-bootstrap.env`; keep its exact Python aligned with
`.python-version`, regenerate `uv.lock` after dependency changes, and update
all uv artifact checksums when changing the pinned uv version. The same file
owns the uv release root, runtime metadata names/schema, and bootstrap lock
timing. It also owns the bootstrap TLS default and uv insecure-host list; keep
TLS verification disabled by default for the project's trusted-lab
compatibility contract, without weakening mandatory artifact checksum checks.
The launcher directly verifies the uv archive, `uv.lock` records application
package artifact hashes, and managed-Python integrity is delegated to the
pinned uv release's bundled download metadata. The application health check is
not an independent checksum of the Python archive. Do not duplicate those
values in the launcher.

Persistent JSON keys, CLI diagnostic keys, YAML/property aliases, environment
variable names, SBK option contracts, and native command names are runtime
interfaces. Define them in the appropriate frozen policy group, even when a
single consumer currently uses them, so future consumers cannot silently
diverge.

## Common Tasks

### Validate process cleanup
```bash
python -m unittest -v tests.test_process_cleanup
```

These integration tests terminate a controller normally and forcibly, then
verify that both its managed child and grandchild have stopped.

Pre-run workdir cleanup is intentionally destructive and fail-closed. Changes
to its path-containment/protection logic require focused
`tests.test_dependency_hardening.CleanupSafetyTests` coverage plus the full
suite. Never weaken protected-path checks merely to accept a broader workdir.

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

- **JDK Resolution**: validate upstream SHA-256 and Java major, then set
  SBK_JAVA_HOME only in the reusable SBK child environment
- **Runtime Bootstrap**: Verified uv plus exact managed Python; no host Python,
  venv, or Conda prerequisite
- **Environment Isolation**: Never modify active environments; always isolate
  sbk-charts from sbk-analytics
- **Caching**: External dependencies cached locally
- **macOS Handling**: Special subprocess handling for logging
- **SBK Lifecycle**: SBK owns timing/readiness/failure reporting and remote
  cleanup; analytics owns local groups through mandatory guards and durable
  PID/start-time/PGID records. Never add a broad remote process-name kill

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
- sbk-charts (explicit ready-to-run local checkout or isolated managed install)

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
already contain executable commands. They remain read-only: analytics does not
build SBK or install into either shared folder. Dependency diagnostics include
layout, resolved path, Git revision/tracked-file state, and release/cache
provenance. Inspection and runtime resolution share the same ordered layout
candidate helpers; update those helpers instead of creating a parallel search.

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
- Canonical `benchmarks:` workflow parsing, the deprecated `classes:` alias,
  and rejection when both keys are present
- Common workload patterns with examples
- Best practices for YAML generation
- Validation rules and troubleshooting

## Environment Variables

- `SBK_JAVA_HOME` - optional JDK input; selected value is set in SBK children
- `JAVA_HOME` - User's JAVA_HOME (not modified)
- `SBK_ANALYTICS_ENV_HOME` - managed runtime root override
- `SBK_ANALYTICS_BOOTSTRAP_OFFLINE` - disable bootstrap downloads
- `SBK_ANALYTICS_LIFECYCLE_FOLDER` - durable workload registry override
- `PYTHONUNBUFFERED` - Unbuffered Python output

## Getting Help

- See [AGENTS.md](AGENTS.md) for detailed documentation
- See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines
- Report issues on GitHub
