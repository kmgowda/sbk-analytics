# Analytics Package

This is the main Python package for sbk-analytics.

## Package Structure

```
analytics/
├── __init__.py           # Package initialization
├── __main__.py           # Entry point for python -m analytics
├── banner.txt            # ASCII art banner
├── cli.py                # Command-line interface
├── config.py             # YAML configuration parsing
├── charts.py             # sbk-charts invocation
├── properties.py         # .env file parsing
├── releases.py           # Dependency resolution (JDK, SBK, sbk-charts)
├── runner.py             # SBK execution (serial/parallel)
├── system_info.py        # System information collection
└── yaml_gen.py           # YAML generation for SBK instances
```

## Module Descriptions

### cli.py
Main entry point for the sbk-analytics CLI. Handles argument parsing, logging setup, and orchestrates the entire workflow.

### config.py
Parses and validates YAML configuration files. Defines the `OrchestratorConfig` class with all benchmark parameters.

### properties.py
Parses .env-style configuration files (like sbk-config.env). Handles case-insensitive key matching.

### releases.py
Resolves and caches external dependencies:
- JDK resolution with priority order (SBK_JAVA_HOME, JAVA_HOME, PATH, cached, download)
- SBK resolution with priority order (explicit local folder, cached, download)
- sbk-charts resolution with priority order (explicit local folder, conda, cached, install)

### runner.py
Executes SBK instances in serial or parallel mode. Handles subprocess management, log forwarding, and hung JVM detection.

### charts.py
Invokes sbk-charts for analytics. Handles AI model parameters and Excel report generation.

### yaml_gen.py
Generates SBK YAML files from configuration. Supports both sbk-yal and sbk-gem-yal formats.

### system_info.py
Collects system information (CPU, RAM, OS, hardware) for Excel reports.

## Key Design Patterns

- **Dependency Injection**: Configuration passed to functions
- **Caching**: External dependencies cached locally
- **Environment Detection**: Auto-detects conda vs venv
- **Error Handling**: Graceful degradation for missing dependencies

## Environment Variables

The package respects these environment variables:
- `SBK_JAVA_HOME` - JDK for SBK execution (set by releases.py)
- `JAVA_HOME` - User's JAVA_HOME (not modified by package)
- `CONDA_PREFIX` - Conda environment detection
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
