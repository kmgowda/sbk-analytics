# Agent Documentation for sbk-analytics

This document provides comprehensive information for AI coding agents (GitHub Copilot, Devin, Cursor, etc.) to understand the sbk-analytics project structure, architecture, and development workflow.

## Project Overview

**sbk-analytics** is an orchestrator for SBK (Storage Benchmark Kit) benchmarks. It automates running multiple SBK benchmark instances and feeds the resulting CSV files into sbk-charts for combined performance analytics.

### Key Purpose
- Automate SBK benchmark execution (serial or parallel modes)
- Resolve and cache dependencies (JDK, SBK, sbk-charts)
- Generate Excel reports with performance charts and AI analysis
- Support both conda and venv environments

## Project Structure

```
sbk-analytics/
├── analytics/                    # Main package directory
│   ├── __init__.py
│   ├── __main__.py              # Entry point for CLI
│   ├── banner.txt               # ASCII art banner
│   ├── cli.py                   # Command-line interface
│   ├── config.py                # YAML configuration parsing
│   ├── charts.py                # sbk-charts invocation
│   ├── properties.py            # .env file parsing
│   ├── releases.py              # Dependency resolution (JDK, SBK, sbk-charts)
│   ├── runner.py                # SBK execution (serial/parallel)
│   ├── system_info.py           # System information collection
│   └── yaml_gen.py              # YAML generation for SBK instances
├── examples/                     # Example configuration files
│   ├── file-rocksdb-write-60s.yml
│   ├── file-rocksdb-write.yml
│   └── config.yml
├── sbk-config.env              # SBK configuration (versions, URLs, folders)
├── environment.yml              # Conda environment specification
├── requirements.txt             # Python dependencies
├── pyproject.toml               # Python package configuration
└── README.md                    # User documentation
```

## Core Components

### 1. CLI Module (`cli.py`)
**Purpose**: Command-line argument parsing and main entry point

**Key Functions**:
- `main()`: Main entry point, orchestrates the entire workflow
- `_parse_args()`: Argument parsing using argparse
- `_setup_logging()`: Logging configuration
- `_print_banner()`: Displays ASCII art banner

**Usage**: Entry point for `sbk-analytics` command

### 2. Config Module (`config.py`)
**Purpose**: Parse and validate YAML configuration files

**Key Classes**:
- `OrchestratorConfig`: Main configuration class with attributes:
  - `mode`: Execution mode (serial/parallel)
  - `sbk_config`: SBK repository and version
  - `classes`: List of benchmark classes to run
  - `workdir`: Working directory for outputs
  - `ai_model`: AI model for analysis
  - `ai_params`: AI parameters
  - `chat`: Chat mode flag

### 3. Properties Module (`properties.py`)
**Purpose**: Parse .env-style configuration files

**Key Functions**:
- `parse_properties()`: Parse .env files into key-value pairs
- Case-insensitive key matching
- Supports dots, underscores, and dashes interchangeably

**Important**: This is used for `sbk-config.env` file parsing

### 4. Releases Module (`releases.py`)
**Purpose**: Dependency resolution and caching

**Key Functions**:
- `ensure_jdk()`: Resolve and cache JDK with priority order:
  1. SBK_JAVA_HOME (highest priority)
  2. JAVA_HOME
  3. java on PATH
  4. Specified jdk folder
  5. Download Temurin JDK
- `ensure_sbk()`: Download and cache SBK releases
- `ensure_sbk_charts()`: Install sbk-charts (conda or venv)

**Key Classes**:
- `JdkInstall`: JDK installation metadata
- `SbkInstall`: SBK installation metadata
- `ChartsInstall`: sbk-charts installation metadata

**Environment Variables Set**:
- `SBK_JAVA_HOME`: Points to resolved JDK (not JAVA_HOME to avoid conflicts)
- `JAVA_TOOL_OPTIONS`: Java system properties for unbuffered output (macOS fix)

### 5. Runner Module (`runner.py`)
**Purpose**: Execute SBK instances in serial or parallel mode

**Key Functions**:
- `run_jobs()`: Main entry point for running SBK instances
- `_run_serial()`: Execute instances one at a time
- `_run_parallel()`: Execute instances concurrently
- `_hung_jvm_watchdog()`: Monitor and kill hung JVM processes

**Environment Configuration**:
- Sets `SBK_JAVA_HOME` to resolved JDK location
- Explicitly unsets `JAVA_HOME` to prevent version conflicts
- Prepends JDK bin directory to PATH
- Sets `JAVA_TOOL_OPTIONS` for unbuffered output on macOS

**macOS Logging Fix**:
- On macOS, captures and forwards SBK logs in real-time
- Uses line-buffered subprocess output
- Sets Java system properties to disable output buffering

### 6. YAML Generator Module (`yaml_gen.py`)
**Purpose**: Generate SBK YAML files from configuration

**Key Functions**:
- `generate_instance_yaml()`: Convert configuration to SBK YAML format
- Handles both sbk-yal and sbk-gem-yal formats

### 7. Charts Module (`charts.py`)
**Purpose**: Invoke sbk-charts for analytics

**Key Functions**:
- `run_sbk_charts()`: Run sbk-charts with all CSV files
- `_prepare_cwd()`: Create custom working directory for sbk-charts
- `_ai_args()`: Convert AI parameters to CLI flags

**Dependencies Required**:
- Pillow>=12.0 (for image handling in Excel files)
- openpyxl-image-loader>=1.0 (for image loading)

### 8. System Info Module (`system_info.py`)
**Purpose**: Collect system information for Excel reports

**Key Functions**:
- `append_system_sheet()`: Add system information sheet to Excel
- Collects CPU, RAM, OS, and hardware details

## Configuration Files

### sbk-config.env
**Purpose**: SBK configuration (versions, URLs, folders)

**Key Settings**:
```
sbk.url=https://github.com/kmgowda/SBK
sbk.version=10.0
sbk.folder=./.sbk
sbk.jdk.version=25
sbk.jdk.folder=./.jdk
ssl.verify=true

sbk-charts.url=https://github.com/kmgowda/sbk-charts
sbk-charts.version=4.26.6.2
```

**JDK Resolution Priority**:
1. SBK_JAVA_HOME (if set and matches version)
2. JAVA_HOME (if set and matches version)
3. java on PATH (if matches version)
4. Specified jdk folder (if cached version matches)
5. Download Temurin JDK

### environment.yml
**Purpose**: Conda environment specification

**Contents**:
- Python 3.10
- PyTorch (from pytorch channel)
- pyyaml
- requests

**Usage**: `conda env create -f environment.yml`

### YAML Configuration Files
**Purpose**: Benchmark configuration

**Structure**:
```yaml
mode: serial  # or parallel
sbk:
  url: https://github.com/kmgowda/SBK
  version: 10.0
classes:
  - name: class_name
    params:
      # SBK-specific parameters
    class: driver_class
```

## Installation and Setup

### For Development
```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install in development mode
pip install -e .
```

### For Conda Users
```bash
# Create conda environment from environment.yml
conda env create -f environment.yml
conda activate sbk-analytics

# Install sbk-analytics
pip install -e .
```

### For Production
```bash
# Install from PyPI (when published)
pip install sbk-analytics
```

## Dependencies

### Required Python Packages
- PyYAML>=6.0
- requests>=2.28
- openpyxl>=3.1
- psutil>=5.9
- Pillow>=12.0 (for Excel image handling)
- openpyxl-image-loader>=1.0 (for image loading)

### External Dependencies
- **JDK**: Temurin/OpenJDK (auto-downloaded)
- **SBK**: Storage Benchmark Kit (auto-downloaded)
- **sbk-charts: Analytics package (auto-downloaded)

### Caching
All external dependencies are cached in:
- JDK: `.jdk/<version>/` or `./.jdk/`
- SBK: `.sbk/<version>/`
- sbk-charts: `.sbk/sbk-charts/<version>/`

## Development Workflow

### Making Changes

1. **Code Style**: Follow PEP 8
2. **Testing**: Run example configurations
3. **Documentation**: Update relevant docstrings and README.md
4. **Dependencies**: Add to requirements.txt and pyproject.toml

### Key Design Decisions

1. **JDK Resolution**: SBK_JAVA_HOME is set (not JAVA_HOME) to avoid conflicts
2. **Environment Detection**: Automatically detects conda vs venv
3. **macOS Logging**: Special handling for Java output buffering
4. **Caching**: External dependencies cached to avoid re-downloads
5. **Error Handling**: Graceful degradation for missing dependencies

### Common Tasks

**Add new benchmark class**:
1. Update YAML configuration schema in `config.py`
2. Add example configuration in `examples/`
3. Test with `sbk-analytics -c examples/new-config.yml`

**Update SBK version**:
1. Modify `sbk.version` in `sbk-config.env`
2. Run `sbk-analytics` - it will auto-download the new version

**Update sbk-charts version**:
1. Modify `sbk-charts.version` in `sbk-config.env`
2. Run `sbk-analytics` - it will auto-install the new version

## Troubleshooting

### Common Issues

**JDK Version Mismatch**:
- Check `sbk.jdk.version` in sbk-config.env
- Verify SBK_JAVA_HOME is not pointing to wrong version
- Let sbk-analytics download the correct JDK

**macOS Logging Missing**:
- Use `--forward-logs` flag
- Check terminal buffering
- Ensure dependencies are installed

**sbk-charts Installation Fails**:
- Check network connectivity
- Verify SSL settings in sbk-config.env
- Try with `ssl.verify=false`

### Debug Mode

Run with verbose flag:
```bash
sbk-analytics -c config.yml -v
```

Run with extra verbose:
```bash
sbk-analytics -c config.yml -vv
```

## Release Process

### Version Bump
1. Update version in `pyproject.toml`
2. Update version in `analytics/__init__.py`
3. Commit changes

### Build Distribution
```bash
python -m build
```

### Test Release
```bash
pip install dist/sbk-analytics-*.tar.gz
sbk-analytics --version
sbk-analytics -c examples/config.yml
```

## Architecture Overview

```
User Input (YML)
       ↓
CLI (cli.py)
       ↓
Config Parser (config.py)
       ↓
Properties Parser (properties.py)
       ↓
JDK Resolution (releases.py)
       ↓
SBK Resolution (releases.py)
       ↓
sbk-charts Installation (releases.py)
       ↓
YAML Generation (yaml_gen.py)
       ↓
SBK Execution (runner.py)
       ↓
CSV Collection
       ↓
sbk-charts Invocation (charts.py)
       ↓
System Info Collection (system_info.py)
       ↓
Excel Output
```

## Important Notes for AI Agents

### When Modifying Code
- Always check if changes affect both conda and venv environments
- Test JDK resolution logic with different JAVA_HOME settings
- Verify macOS compatibility for any subprocess handling
- Ensure dependency caching works correctly

### When Adding Features
- Consider impact on existing configurations
- Add examples to the `examples/` directory
- Update README.md with usage instructions
- Check if new dependencies need to be added to requirements.txt

### When Debugging Issues
- Use verbose flags to see detailed logging
- Check cache directories for dependency issues
- Verify environment variable settings
- Test with both serial and parallel modes

### File Naming Conventions
- Configuration files: `*.yml`, `*.env`
- Python modules: `*.py`
- Documentation: `*.md`
- Example files: `examples/`

### Environment Variables
- `CONDA_PREFIX`: Detects conda environment
- `SBK_JAVA_HOME`: Points to JDK for SBK (set by sbk-analytics)
- `JAVA_HOME`: User's JAVA_HOME (not modified by sbk-analytics)
- `PYTHONUNBUFFERED`: For unbuffered Python output

### Exit Codes
- `0`: Success
- `1`: Configuration error
- `2`: Dependency resolution failure
- Other: SBK execution errors

## Contact and Support

- **GitHub**: https://github.com/kmgowda/sbk-analytics
- **Issues**: Report bugs via GitHub Issues
- **License**: Apache-2.0

## Version History

- **0.1.0**: Initial release
  - JDK resolution with priority order
  - Conda and venv support
  - macOS logging fixes
  - Excel output with system information
  - AI analytics integration

## Related Documentation

- **[README.md](../README.md)** - User documentation and quick start guide
- **[CONTRIBUTING.md](CONTRIBUTING.md)** - Contribution guidelines
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - High-level architecture overview
- **[DEVELOPMENT.md](DEVELOPMENT.md)** - Quick development reference
- **[SUPPORT.md](SUPPORT.md)** - Help and troubleshooting guide