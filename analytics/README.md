# Analytics Package

This is the main Python package for sbk-analytics.

## Package Structure

```
analytics/
├── __init__.py           # Package initialization
├── __main__.py           # Entry point for python -m analytics
├── banner.txt            # ASCII art banner
├── cli.py                # Command-line interface
├── workflow.py           # Benchmark/report execution pipeline
├── config.py             # YAML configuration parsing
├── charts.py             # sbk-charts invocation
├── properties.py         # .env file parsing
├── policy.py             # runtime policy and artifact metadata
├── releases/             # Dependency resolution package
│   ├── __init__.py       # Stable public resolver API
│   ├── _shared.py        # Cache, download, archive, provenance primitives
│   ├── sbk.py            # SBK resolution
│   ├── charts.py         # sbk-charts resolution
│   └── jdk.py            # JDK resolution
├── lifecycle.py          # Durable workload ownership and reconciliation
├── runner.py             # SBK execution (serial/parallel)
├── system_info.py        # System information collection
└── yaml_gen.py           # YAML generation for SBK instances
```

## Module Descriptions

### cli.py and workflow.py
`cli.py` handles argument parsing, logging, diagnostics, and command dispatch.
`workflow.py` owns the ordered dependency, benchmark, chart, system-info, and
cleanup pipeline through injected service boundaries.

### config.py
Parses and validates YAML configuration files. Defines the `OrchestratorConfig` class with all benchmark parameters.

### properties.py
Parses .env-style configuration files (like sbk-config.env). Handles case-insensitive key matching.

### policy.py
Defines immutable application/artifact metadata and cross-cutting runtime
policy for dependency layouts and provenance, executable/environment names,
command interfaces, configuration/property aliases, persistent metadata and
diagnostic schemas, cache/network operations, display units, process and
benchmark timing, host-platform identities, generated workflow filenames,
Java runtime options, SSH/native probe behavior, configuration defaults, and
exit codes.

### releases package
Resolves and caches external dependencies:
- JDK resolution with priority order plus upstream checksum, executable, and
  exact-major validation before managed publication
- SBK resolution with priority order (explicit local folder, cached, download)
- sbk-charts resolution with priority order (explicit local folder, verified
  isolated cache, install)
- read-only shared-folder inspection and release/workspace provenance reporting

`analytics.releases` remains the stable public import facade. Artifact-specific
managed and local resolution/inspection lives in `releases/sbk.py`,
`releases/charts.py`, and `releases/jdk.py`; `_shared.py` contains common
models plus cache, network, archive-safety, and provenance primitives.

SBK and sbk-charts are dependency providers, not Git subprojects of
sbk-analytics. Managed workflows consume pinned GitHub releases; development
workflows may select ready-to-run shared folders. Compilation and packaging
remain owned by the dependency repositories. The orchestrator must not require
Git submodule initialization or build SBK during bootstrap or execution. See
the dependency-provider decision in `ARCHITECTURE.md` for the rationale and
supported integration boundary.

### runner.py
Executes SBK instances in serial or parallel mode. Handles subprocess
management and log forwarding while SBK owns its native lifecycle.

### charts.py
Invokes sbk-charts for analytics. Handles AI model parameters and Excel report generation.

### yaml_gen.py
Generates SBK YAML files from configuration. Supports both sbk-yal and sbk-gem-yal formats.

### system_info.py
Collects system information (CPU, RAM, OS, hardware) for Excel reports.

## Key Design Patterns

- **Dependency Injection**: Configuration passed to functions
- **Caching**: External dependencies cached locally
- **Self-contained runtime**: verified uv, exact managed Python, and `uv.lock`
- **Environment isolation**: active environments are untouched and sbk-charts
  has a dedicated cached environment
- **Error Handling**: Graceful degradation for missing dependencies

## Environment Variables

The package respects these environment variables:
- `SBK_JAVA_HOME` - optional JDK input and child-only SBK execution setting
- `SBK_ANALYTICS_LIFECYCLE_FOLDER` - durable local workload registry override
- `JAVA_HOME` - User's JAVA_HOME (not modified by package)
- `SBK_ANALYTICS_ENV_HOME` - persistent managed runtime override
- `SBK_ANALYTICS_BOOTSTRAP_OFFLINE` - disable bootstrap downloads
- `SBK_ANALYTICS_BOOTSTRAP_TLS_VERIFY=false` (in `sbk-bootstrap.env`) - shipped
  default that disables certificate verification for curl/wget and uv while
  retaining mandatory uv archive checksum verification; `uv.lock` records
  application-package artifact hashes, while managed-Python integrity remains
  delegated to the pinned uv release rather than a second application checksum
- `PYTHONUNBUFFERED` - Unbuffered Python output

## Dependencies

See `requirements.txt` and `pyproject.toml` for Python dependencies.

## Entry Points

- CLI: `sbk-analytics` command (defined in pyproject.toml)
- Module: `python -m analytics` (uses __main__.py)

For detailed documentation, see:
- **[AGENTS.md](AGENTS.md)** - Comprehensive AI agent documentation
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - High-level architecture overview
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Contribution guidelines
- **[DEVELOPMENT.md](DEVELOPMENT.md)** - Quick development reference
- **[SUPPORT.md](SUPPORT.md)** - Help and troubleshooting guide
- **[CHANGELOG.md](CHANGELOG.md)** - Version history
- **[README.md](../README.md)** - Main project documentation
