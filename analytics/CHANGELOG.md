# Changelog

All notable changes to sbk-analytics will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- SBK 10.6+ option compatibility, including validation for its GEM/SBM
  aggregate, cleanup, provisioning, host-key, port, and idle-timeout options.
- Read-only dependency provenance diagnostics for shared development folders
  and managed GitHub releases, including layouts, resolved commands, Git state,
  release assets, checksums, and machine-readable status details.

### Changed
- Extended immutable runtime policy coverage to dependency layouts and source
  vocabulary, executable/environment names, SBK and charts interfaces, pip
  invocation, diagnostic formatting, units, and signal exit conventions.
- Kept shared-folder Git inspection lightweight by excluding untracked files,
  added debug logging for Git failures, and unified status/runtime layout order.
- Established SBK 10.6 as the configured baseline contract without embedding
  version checks or version-specific branches in runtime source.
- Replaced text-rendered architecture and lifecycle flows with GitHub-rendered
  Mermaid diagrams in the user, architecture, and agent documentation.
- Delegated benchmark timing, fixed-record idle detection, GEM readiness,
  remote lifecycle, and failure status to SBK 10.6+. Emergency SSH cleanup now
  runs only when interrupted SBK-GEM does not finish native cleanup in time.
- Require both exit code zero and a non-empty CSV before charting an SBK run.

### Fixed
- Resolve configured plain release versions such as `10.6` against upstream
  GitHub tags that use the conventional `v` prefix, such as `v10.6`.
- Prevented analytics from killing SBK-GEM while 10.6 is still provisioning
  nodes before the benchmark timer starts.

### Removed
- Native Windows launcher, bootstrap checksums, CI job, packaging, tests, and
  runtime-specific process/cache branches. Supported application targets are
  now Linux and macOS.

## [1.26.8.2] - 2026-08-22

### Added
- Fully self-contained Bash and PowerShell launchers for Linux, macOS, and
  Windows using pinned, checksum-verified uv artifacts and exact uv-managed
  Python, with argument forwarding and exit-code preservation
- Cross-platform `uv.lock`, persistent per-user application state, atomic
  fingerprinted environments, concurrent bootstrap locking, offline reuse,
  corruption repair, and interrupted-install recovery tests
- Optional `sbk-charts.sha256`; the shipped configuration verifies the tag
  archive and installs it without requiring system Git
- Unified extensionless `sbk-analytics` application that selects the native
  bootstrap launcher on Linux, macOS, and Windows-compatible POSIX shells
- Managed child-process trees for `sbk-yal`, `sbk-gem-yal`, and `sbk-charts`,
  including signal cleanup, POSIX parent-death guards, Windows kill-on-close
  jobs, and forced-exit integration tests
- Explicit `sbk.local.folder` and `sbk-charts.local.folder` resolution with
  local-first precedence, fail-fast validation, and dependency-source output
- AGENTS.md documentation for AI coding assistants
- CONTRIBUTING.md for contributors and AI agents
- CHANGELOG.md for version history tracking
- `deps doctor`, read-only `deps status`, `config init`, `--resolve-only`, and
  JSON dependency summaries
- CLI/environment overrides, direct sbk-charts executable selection, version
  policies, custom CA bundles, and a fast local end-to-end smoke example
- Linux/macOS Python 3.9/3.12 CI and archive/cache security tests
- Opt-in, workdir-confined file benchmark cleanup and disk-space reporting

### Fixed
- Launcher fingerprints now cover all runtime source and configuration inputs,
  and new environments force a local package rebuild so cached wheels cannot
  report or execute an older sbk-analytics version
- Made SSH null-device handling and the default benchmark work directory
  platform-aware, and added clear validation errors for malformed native
  launcher policy values
- Catchable signals now preserve the one-document `--json` contract and their
  `128 + signal` exit code; pip installer and parallel progress output are
  explicitly routed to stderr
- POSIX cleanup tolerates permission-denied process groups without interrupting
  the remaining managed-process sweep
- CI portability across Python 3.9, macOS, and Windows by using a
  Python-compatible Pillow constraint, canonical path assertions, and
  console-safe dependency status markers
- SBK logo missing in Excel reports by adding a Python-compatible Pillow
  dependency and openpyxl-image-loader>=1.0
- JDK resolution logic to prioritize SBK_JAVA_HOME over JAVA_HOME to avoid version conflicts
- macOS logging issues with --forward-logs flag for real-time log forwarding
- Cache precedence, strict TLS boolean parsing, conda/local version reporting,
  archive traversal/link extraction, corrupt cache recovery, and concurrent
  installer races

### Changed
- Active venv/Conda environments are no longer modified by the native launcher;
  sbk-analytics and sbk-charts always use separate managed environments
- Production bootstrap installs sbk-analytics non-editably and runs Python in
  safe-path mode with `PYTHONPATH` and `PYTHONHOME` cleared
- Separated the Temurin repository identity from its parameterized JDK download
  URL template and documented the compatibility/security implications of the
  disabled TLS and SSH verification defaults
- Centralized application/dependency metadata, cache layout, network behavior,
  process/benchmark/SSH timing, configuration defaults, and exit codes in
  immutable typed runtime policy objects
- Centralized shared Bash/PowerShell environment names, interpreter versions,
  and bootstrap marker metadata in `sbk-bootstrap.env`
- Updated documentation to clarify JDK resolution priority order
- Enhanced README.md with AI agent documentation section
- Local SBK is validated before JDK/network work, and sbk-charts resolution is
  delayed until usable CSV input exists
- Managed installations are staged, validated, recorded in metadata, and
  atomically published on POSIX (lock-coordinated on Windows); the `.ok`
  marker is written last
- TLS verification remains disabled by default as configured by the project
- `--json` now reserves stdout for exactly one JSON document and sends human
  progress plus child-process output to stderr
- Cache documentation distinguishes POSIX atomic publication from
  lock-coordinated Windows publication

## [1.26.6.1] - 2026-06-14

### Added
- Apache 2.0 license headers to all Python source files
- Project configuration files (.editorconfig, GitHub templates)
- Comprehensive documentation for AI agents (AGENTS.md)
- Architecture documentation (ARCHITECTURE.md)
- Contribution guidelines (CONTRIBUTING.md)
- Development reference (DEVELOPMENT.md)
- Support documentation (SUPPORT.md)
- Single source of truth for version management

### Changed
- Version updated to 1.26.6.1
- Dynamic versioning from package __init__.py
- Enhanced README.md with AI agent documentation section

### Fixed
- SBK logo missing in Excel reports by adding Pillow and image loader dependencies
- JDK resolution logic to prioritize SBK_JAVA_HOME over JAVA_HOME to avoid version conflicts
- macOS logging issues with --forward-logs flag for real-time log forwarding
- Documentation to reflect actual cache implementation with extracted/ subdirectories
- Configuration table to show correct defaults for project-local folders

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
