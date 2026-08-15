# Architecture Overview

This document provides a high-level architectural overview of sbk-analytics.

## System Architecture

The extensionless `sbk-analytics` application is the canonical launcher. It
dispatches to `sbk-analytics.sh` on Linux/macOS or `sbk-analytics.ps1` from a
Windows-compatible POSIX shell. Native PowerShell invokes the `.ps1` launcher
directly. These bootstrap layers reuse an active compatible venv/Conda
environment, reuse or create a launcher-managed environment, and install the
checkout when dependency inputs change. Bash replaces itself with
`python -m analytics`; PowerShell waits for Python and propagates its exit code.

`policy.py` is the dependency-free policy boundary used by the CLI,
configuration parser, resolver, runner, process manager, charts adapter, and
system-info collector. Immutable typed policy groups centralize values that
cross module boundaries, while `sbk-config.env` remains the operator-controlled
source for release version pins and local dependency selections.
The native launchers load their smaller pre-Python policy boundary from
`sbk-bootstrap.env`, because `analytics.policy` cannot be imported until a
compatible interpreter and environment exist.

```
┌─────────────────────────────────────────────────────────────┐
│                        User Input                           │
│                    (YAML Configuration)                     │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    CLI Module (cli.py)                      │
│  - Argument parsing                                         │
│  - Logging setup                                            │
│  - Workflow orchestration                                   │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                Configuration Module (config.py)              │
│  - YAML parsing and validation                              │
│  - Configuration object creation                             │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│              Properties Module (properties.py)               │
│  - sbk-config.env parsing                                   │
│  - Version and URL resolution                               │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│              Releases Module (releases.py)                  │
│  - JDK resolution and caching                               │
│  - SBK download and caching                                │
│  - sbk-charts installation                                  │
└─────────────────────────┬───────────────────────────────────┘
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
        ▼                                   ▼
┌──────────────────┐              ┌──────────────────┐
│ JDK Installation │              │ SBK Installation │
│  (.jdk/)         │              │   (.sbk/)        │
└──────────────────┘              └──────────────────┘
        │                                   │
        └─────────────────┬─────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│              YAML Generator (yaml_gen.py)                   │
│  - Generate per-class YAML files                             │
│  - Handle sbk-yal/sbk-gem-yal formats                        │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Runner Module (runner.py)                   │
│  - Serial/parallel execution                                │
│  - Subprocess management                                    │
│  - Log forwarding (macOS)                                   │
│  - Hung JVM detection                                       │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│          Process Lifecycle Module (processes.py)             │
│  - Isolated workload process trees                           │
│  - Signal and parent-death cleanup                           │
│  - POSIX liveness guard / Windows Job Object                 │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    CSV Collection                           │
│  - Collect successful CSV files                             │
│  - Filter failed instances                                  │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Charts Module (charts.py)                    │
│  - sbk-charts invocation                                     │
│  - AI parameter handling                                    │
│  - Excel report generation                                   │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│             System Info Module (system_info.py)              │
│  - CPU, RAM, OS collection                                   │
│  - Hardware information                                     │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                    Excel Output                              │
│  - Performance charts                                       │
│  - AI analytics                                             │
│  - System information sheet                                 │
└─────────────────────────────────────────────────────────────┘
```

## Component Interactions

### 1. Configuration Flow
```
User YAML → cli.py → config.py → OrchestratorConfig
sbk-config.env → properties.py → releases.py
```

### 2. Dependency Resolution Flow
```
releases.py → ensure_sbk() → local SBK or locked/staged cache download
releases.py → ensure_jdk() → installed JDK or locked/staged cache download
usable CSVs → ensure_sbk_charts() → local, conda, or locked/staged cache
```

Explicit local SBK validation happens before JDK resolution. sbk-charts is
lazy during normal runs, so a failed SBK workload does not trigger a charts
install. `deps doctor` intentionally resolves and starts all three tools.
Archive members are checked before extraction, and managed installs are
published only after executable validation and metadata creation. Directory
publication is atomic on POSIX. On Windows it is coordinated by the same
per-version lock, but is not documented as an atomic directory replacement.

### 3. Execution Flow
```
OrchestratorConfig → yaml_gen.py → SBK YAML files
SBK YAML files → runner.py → SBK instances
SBK instances → CSV files
CSV files → charts.py → sbk-charts → Excel report
```

### 4. Post-Processing Flow
```
Excel report → system_info.py → System sheet
System sheet → Final Excel output
```

### 5. Process Lifecycle Flow
```
cli.py signal context → runner.py / charts.py → managed process tree
catchable signal → workload-specific cleanup → TERM → 3 s grace → KILL
POSIX parent death → liveness-pipe EOF → independent guard → TERM/KILL group
Windows parent death → Job Object handle close → kill complete job tree
```

Every SBK and sbk-charts invocation is registered until its complete process
tree exits. Normal wrapper exit also triggers removal of any remaining
descendants in its workload group. Catchable sbk-gem interruptions retain best-effort remote SSH
cleanup; no local mechanism can initiate new remote cleanup after SIGKILL.

## Key Design Decisions

### 1. JDK Resolution Priority
**Decision**: SBK_JAVA_HOME > JAVA_HOME > PATH > cached > download

**Rationale**: 
- Allows explicit JDK specification via SBK_JAVA_HOME
- Respects user's JAVA_HOME but doesn't override it
- Provides fallback to PATH and cached versions
- Auto-downloads as last resort

### 2. Environment Separation
**Decision**: Set SBK_JAVA_HOME, not JAVA_HOME

**Rationale**:
- Avoids conflicts with user's JAVA_HOME
- Allows SBK to use specific JDK version
- Maintains user's environment integrity

### 3. Caching Strategy
**Decision**: Cache all external dependencies

**Rationale**:
- Reduces network requests
- Improves performance
- Enables offline operation after initial download
- Simplifies version management

### 4. Execution Modes
**Decision**: Support both serial and parallel execution

**Rationale**:
- Serial: Easier debugging, resource-friendly
- Parallel: Faster execution for independent benchmarks
- User choice based on requirements

### 5. sbk-charts Invocation
**Decision**: Invoke once with all CSVs, not per-instance

**Rationale**:
- Combined analytics across all instances
- Single Excel report with comparisons
- More efficient resource usage
- Better for trend analysis

## Data Flow

### Configuration Data
```
YAML config → OrchestratorConfig object
sbk-config.env → Properties dict
```

### Execution Data
```
YAML files → SBK instances → CSV files
CSV files → sbk-charts → Excel report
```

### System Data
```
System info → System sheet → Excel report
```

## Error Handling Strategy

### Graceful Degradation
- Failed SBK instances don't stop execution
- Partial CSV collection still triggers sbk-charts
- Missing dependencies trigger auto-download

### Error Propagation
- Configuration errors: Fail fast
- Dependency errors: Clear error messages
- Execution errors: Log and continue if possible

### Logging Strategy
- Configurable verbosity (-v, -vv)
- Separate logs for parallel execution
- Real-time log forwarding on macOS

## Performance Considerations

### Caching
- JDK: Cached by version
- SBK: Cached by version
- sbk-charts: Cached by version
- Explicit local SBK/sbk-charts folders bypass managed caches and are never modified

### Parallel Execution
- Concurrent SBK instances
- Independent CSV collection
- Single sbk-charts invocation

### Resource Management
- Hung JVM detection and cleanup
- Process monitoring
- Resource cleanup on exit

## Security Considerations

### Dependency Sources
- GitHub releases (SBK, sbk-charts)
- Temurin JDK (official builds)
- SSL verification (configurable)

### File Operations
- Workdir isolation
- Permission checks
- Safe file handling

### Environment Variables
- SBK_JAVA_HOME set by tool
- JAVA_HOME not modified
- User environment respected

## Extensibility Points

### Adding New Benchmark Classes
1. Update `config.py` schema
2. Update `yaml_gen.py` generation logic
3. Add example configurations

### Adding New Analytics
1. Update `charts.py` parameters
2. Update sbk-charts invocation
3. Update documentation

### Adding New Platforms
1. Update `runner.py` platform detection
2. Add platform-specific handling
3. Test and document

## Technology Stack

### Python
- **Version**: 3.9+
- **Package Manager**: pip/conda
- **Key Libraries**: PyYAML, requests, openpyxl, psutil

### External Dependencies
- **JDK**: Temurin/OpenJDK
- **SBK**: Java-based storage benchmark
- **sbk-charts**: Python-based analytics

### Platforms
- **Primary**: Linux
- **Supported**: macOS (with special handling)
- **Experimental**: Windows

## Monitoring and Observability

### Logging
- Structured logging with levels
- Configurable verbosity
- Per-instance logs (parallel mode)

### Metrics
- Execution time per instance
- Success/failure rates
- Resource usage (via system_info)

### Debugging
- Verbose mode for detailed logs
- Log file preservation
- Error context preservation

For more detailed information, see [AGENTS.md](AGENTS.md).
