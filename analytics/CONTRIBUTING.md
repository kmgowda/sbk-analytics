# Contributing to sbk-analytics

Thank you for your interest in contributing to sbk-analytics! This document provides guidelines for contributors and AI coding assistants.

## Development Setup

### Prerequisites
- Python 3.9 or higher
- Git
- Conda (optional but recommended)

### Setting Up Development Environment

```bash
# Clone the repository
git clone https://github.com/kmgowda/sbk-analytics.git
cd sbk-analytics

# Create conda environment (recommended)
conda env create -f environment.yml
conda activate sbk-analytics

# Or use venv
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install in development mode
pip install -e .

# Verify installation
sbk-analytics --version
```

## Project Structure for Contributors

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
│   ├── releases.py              # Dependency resolution
│   ├── runner.py                # SBK execution
│   ├── system_info.py           # System information collection
│   └── yaml_gen.py              # YAML generation
├── examples/                     # Example configurations
├── sbk-config.env              # SBK configuration
├── environment.yml              # Conda environment
├── requirements.txt             # Python dependencies
├── pyproject.toml               # Package configuration
├── AGENTS.md                    # AI agent documentation
└── README.md                    # User documentation
```

## Making Changes

### Code Style
- Follow PEP 8 style guidelines
- Use meaningful variable and function names
- Add docstrings to functions and classes
- Keep functions focused and modular

### Testing Changes
1. Test with example configurations:
   ```bash
   sbk-analytics -c examples/file-rocksdb-write-60s.yml
   ```

2. Test with verbose logging:
   ```bash
   sbk-analytics -c examples/config.yml -v
   ```

3. Test on multiple platforms (Linux, macOS, Windows if possible)

### Documentation Updates
- Update relevant docstrings in code
- Update README.md if user-facing changes
- Update AGENTS.md if architectural changes
- Add examples to `examples/` directory for new features

## Key Areas for Contribution

### 1. Adding New Benchmark Classes
1. Update YAML schema in `analytics/config.py`
2. Add example configuration in `examples/`
3. Test with both serial and parallel modes
4. Update documentation

### 2. Improving JDK Resolution
- Modify `analytics/releases.py:ensure_jdk()`
- Test with different JAVA_HOME settings
- Test on different platforms
- Update documentation

### 3. Enhancing Logging
- Modify `analytics/runner.py` for subprocess handling
- Test on macOS (special handling required)
- Add new CLI flags if needed
- Update README.md

### 4. Adding New sbk-charts Features
- Modify `analytics/charts.py`
- Test with different AI models
- Update dependency list if needed
- Add examples

## Pull Request Process

### Before Submitting
1. Run tests with example configurations
2. Check for linting issues
3. Update documentation
4. Ensure backward compatibility

### PR Description Template
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Tested with example configurations
- [ ] Tested on Linux
- [ ] Tested on macOS
- [ ] Tested on Windows (if applicable)

## Documentation
- [ ] Updated README.md
- [ ] Updated AGENTS.md
- [ ] Added examples
```

## Common Issues and Solutions

### JDK Version Mismatch
**Problem**: SBK fails with `UnsupportedClassVersionError`

**Solution**: 
- Check `sbk.jdk.version` in `sbk-config.env`
- Verify SBK_JAVA_HOME is not set to wrong version
- Let sbk-analytics auto-download correct JDK

### macOS Logging Missing
**Problem**: SBK logs not visible on macOS

**Solution**:
- Use `--forward-logs` flag
- Check terminal buffering settings
- Ensure dependencies are installed

### sbk-charts Installation Fails
**Problem**: sbk-charts installation fails

**Solution**:
- Check network connectivity
- Verify SSL settings in `sbk-config.env`
- Try with `ssl.verify=false`

## Development Guidelines for AI Agents

### When Modifying Code
1. Always check impact on both conda and venv environments
2. Test JDK resolution logic with different JAVA_HOME settings
3. Verify macOS compatibility for subprocess handling
4. Ensure dependency caching works correctly

### When Adding Features
1. Consider impact on existing configurations
2. Add examples to `examples/` directory
3. Update README.md with usage instructions
4. Check if new dependencies needed in requirements.txt

### When Debugging
1. Use verbose flags: `-v` or `-vv`
2. Check cache directories for dependency issues
3. Verify environment variable settings
4. Test with both serial and parallel modes

## Environment Variables

### Important Variables
- `CONDA_PREFIX`: Detects conda environment
- `SBK_JAVA_HOME`: Points to JDK for SBK (set by sbk-analytics)
- `JAVA_HOME`: User's JAVA_HOME (not modified by sbk-analytics)
- `PYTHONUNBUFFERED`: For unbuffered Python output

### JDK Resolution Priority
1. SBK_JAVA_HOME (highest priority)
2. JAVA_HOME
3. java on PATH
4. Specified jdk folder
5. Download Temurin JDK

## Release Process

### Version Bump
1. Update version in `pyproject.toml`
2. Update version in `analytics/__init__.py`
3. Update CHANGELOG.md
4. Commit changes

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

### Publish to PyPI
```bash
python -m twine upload dist/*
```

## Getting Help

- **Documentation**: See [AGENTS.md](AGENTS.md) for detailed technical documentation
- **Issues**: Report bugs via GitHub Issues
- **Discussions**: Use GitHub Discussions for questions

## Code of Conduct

Be respectful and constructive in all interactions. We welcome contributions from everyone regardless of background or experience level.

## License

By contributing to sbk-analytics, you agree that your contributions will be licensed under the Apache-2.0 License.