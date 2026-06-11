# Changelog

All notable changes to sbk-analytics will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- AGENTS.md documentation for AI coding assistants
- CONTRIBUTING.md for contributors and AI agents
- CHANGELOG.md for version history tracking

### Fixed
- SBK logo missing in Excel reports by adding Pillow>=12.0 and openpyxl-image-loader>=1.0 dependencies
- JDK resolution logic to prioritize SBK_JAVA_HOME over JAVA_HOME to avoid version conflicts
- macOS logging issues with --forward-logs flag for real-time log forwarding

### Changed
- Updated documentation to clarify JDK resolution priority order
- Enhanced README.md with AI agent documentation section

## [0.1.0] - 2025-12-XX

### Added
- Initial release of sbk-analytics
- JDK resolution with 5-step priority order (SBK_JAVA_HOME, JAVA_HOME, PATH, cached, download)
- SBK release download and caching
- sbk-charts installation (conda and venv support)
- YAML configuration parsing for benchmark runs
- Serial and parallel execution modes
- CSV file collection from SBK instances
- sbk-charts invocation for Excel report generation
- System information collection (CPU, RAM, hardware)
- Post-processing to append system sheet to Excel
- Example configuration files
- Comprehensive documentation (README.md)
- macOS compatibility with unbuffered Java output
- Conda and venv environment detection
- Dependency caching for JDK, SBK, and sbk-charts
- AI analytics integration with sbk-charts

### Key Features
- Automated SBK benchmark execution
- Combined analytics from multiple benchmark runs
- Excel output with charts and AI analysis
- Cross-platform support (Linux, macOS, Windows)
- Environment management (conda/venv)
- Dependency resolution and caching
- System information collection

## Future Plans

### Planned Features
- [ ] Docker support for containerized execution
- [ ] More comprehensive error handling
- [ ] Additional benchmark class support
- [ ] Performance metrics dashboard
- [ ] Automated testing framework
- [ ] CI/CD pipeline integration
- [ ] Plugin system for custom analyzers

### Known Issues
- JDK version compatibility with SBK compiled versions
- macOS terminal buffering edge cases
- Windows compatibility testing needed

---

For more information, see [AGENTS.md](AGENTS.md) for detailed technical documentation and [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.