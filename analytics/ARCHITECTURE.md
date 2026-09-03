# Architecture Overview

This document provides a high-level architectural overview of sbk-analytics.

## System Architecture

The extensionless `sbk-analytics` application is the canonical Linux/macOS
launcher and dispatches to `sbk-analytics.sh`. Native Windows is not supported.
These stage-zero layers need no system Python or Conda: they acquire
a pinned uv binary with a checked-in platform SHA-256, install exact managed
Python, and build a non-editable `uv.lock` environment in persistent per-user
state. Fingerprinted environments are lock-coordinated, health-checked, and
staged before publication. Runtime source and configuration inputs participate
in the fingerprint, and each new environment forces a fresh local-package
build rather than accepting a cached wheel. Bash replaces itself with safe-path Python.

`policy.py` is the dependency-free policy boundary used by the CLI,
configuration parser, resolver, runner, process manager, charts adapter, and
system-info collector. Immutable typed policy groups centralize values that
cross module boundaries, including dependency layouts/provenance, executable
and environment names, configuration aliases, SBK option contracts,
cache/lifecycle/diagnostic schemas, native command interfaces, units, status
vocabulary, and timeouts. `sbk-config.env` remains the operator-controlled
source for release version pins and local dependency selections.

Lifecycle schemas intentionally fail closed. Records from unsupported schema
versions are quarantined instead of migrated automatically, because an older
record may lack the process identity evidence required for safe termination.
Restricted access to a process environment is logged at debug level and leaves
ambiguous leaderless groups untouched.

The native launchers load their smaller pre-Python policy boundary from
`sbk-bootstrap.env`, because `analytics.policy` cannot be imported until a
compatible interpreter and environment exist.

```mermaid
flowchart TB
    User["User input<br/>Benchmark YAML + sbk-config.env"]

    subgraph Bootstrap["Self-bootstrapping application"]
        Launcher["sbk-analytics<br/>Platform dispatch"]
        Runtime["sbk-analytics.sh<br/>Verified uv + managed Python"]
    end

    subgraph Orchestration["Analytics orchestration"]
        CLI["cli.py<br/>Arguments, logging, dispatch"]
        Workflow["workflow.py<br/>Execution pipeline"]
        Config["config.py + sbk_contract.py<br/>Parse, normalize, validate"]
        Properties["properties.py<br/>Release and local-package settings"]
        Resolver["releases package<br/>Shared cache and provenance"]
        ArtifactResolvers["releases/sbk.py + charts.py + jdk.py<br/>Artifact-specific resolution"]
        Generator["yaml_gen.py<br/>Generate sbkArgs / sbkGemArgs YAML"]
        Runner["runner.py<br/>Serial or parallel SBK execution"]
        Processes["processes.py + lifecycle.py<br/>Managed trees and durable ownership"]
        CSV["Successful CSV results<br/>Exit code 0 + non-empty file"]
        Charts["charts.py<br/>Single sbk-charts invocation"]
        SystemInfo["system_info.py<br/>Host and remote system data"]
        Report["Excel report<br/>Charts, analysis, system sheet"]
    end

    subgraph Managed["Resolved dependencies"]
        JDK["JDK<br/>Installed, cached, or downloaded"]
        SBK["SBK<br/>Local or managed release"]
        SBKCharts["sbk-charts<br/>Local or isolated managed environment"]
    end

    Policy["policy.py<br/>Immutable runtime policy and artifact metadata"]

    User --> Launcher --> Runtime --> CLI
    User --> Config
    User --> Properties
    CLI --> Config
    CLI --> Properties --> Workflow --> Resolver --> ArtifactResolvers
    Config --> Generator
    Resolver --> JDK
    Resolver --> SBK
    JDK --> Runner
    SBK --> Runner
    Generator --> Runner --> Processes --> CSV
    CSV -->|At least one usable input| SBKCharts --> Charts
    CSV --> Charts --> Report
    SystemInfo --> Report
    Policy -. shared policy .-> CLI
    Policy -.-> Workflow
    Policy -.-> Resolver
    Policy -.-> Runner
    Policy -.-> Processes
```

## Component Interactions

`workflow.py` keeps the top-level coordinator intentionally small. Each phase
has one responsibility and can be tested directly through `WorkflowServices`.

```mermaid
flowchart LR
    Prepare["Prepare config and workdir"] --> Resolve["Resolve SBK and JDK"]
    Resolve --> Mode{"Doctor / resolve-only?"}
    Mode -->|Yes| Doctor["Resolve and preflight sbk-charts"]
    Mode -->|No| Benchmarks["Generate YAML and run SBK workloads"]
    Benchmarks --> Inputs["Collect and validate usable CSV inputs"]
    Inputs -->|None| Stop["Return no-usable-CSV status"]
    Inputs -->|Available| Report["Resolve charts and publish report"]
    Report --> System["Append system information and cleanup"]
```

### 1. Configuration Flow
```mermaid
flowchart LR
    YAML["Benchmark YAML"] --> CLI["cli.py"] --> Config["config.py"]
    Config --> Contract["sbk_contract.py"] --> Model["OrchestratorConfig"]
    Env["sbk-config.env"] --> Properties["properties.py"] --> Versions["Versions"]
```

### 2. Dependency Resolution Flow
```mermaid
flowchart TB
    Resolver["releases package facade"]

    Resolver --> EnsureSBK["releases/sbk.py<br/>ensure_sbk()"]
    EnsureSBK --> LocalSBK{"Local SBK configured?"}
    LocalSBK -->|Yes| ValidateSBK["Read-only validation<br/>No SBK build"]
    LocalSBK -->|No| CachedSBK{"Complete managed cache?"}
    CachedSBK -->|Yes| ReuseSBK["Reuse cached SBK"]
    CachedSBK -->|No| DownloadSBK["Lock, download, validate, publish"]

    Resolver --> EnsureJDK["releases/jdk.py<br/>ensure_jdk()"]
    EnsureJDK --> ExistingJDK{"Matching JDK available?"}
    ExistingJDK -->|Yes| ReuseJDK["Use installed or cached JDK"]
    ExistingJDK -->|No| DownloadJDK["Resolve upstream checksum<br/>download and verify Temurin"]
    DownloadJDK --> ValidateJDK["Run java -version<br/>require configured major"]

    CSV["At least one successful CSV"] --> EnsureCharts["releases/charts.py<br/>ensure_sbk_charts()"]
    EnsureCharts --> LocalCharts{"Local charts selected?"}
    LocalCharts -->|Yes| ValidateCharts["Read-only validation<br/>No install"]
    LocalCharts -->|No| CachedCharts{"Complete charts cache?"}
    CachedCharts -->|Yes| ReuseCharts["Reuse isolated environment"]
    CachedCharts -->|No| InstallCharts["Verify source and install"]
```

Explicit local SBK validation happens before JDK resolution. sbk-charts is
lazy during normal runs, so a failed SBK workload does not trigger a charts
install. `deps doctor` intentionally resolves and starts all three tools.
Shared-folder resolution never builds SBK or performs a managed release install
into either checkout; the owning development workflow must prepare runnable
commands first. A selected sbk-charts source launcher owns its isolated runtime.
Archive members are checked before extraction, and managed installs are
published only after executable validation and metadata creation. Directory
publication is atomic and coordinated by a per-version lock.

### Architectural decision: dependency providers, not Git subprojects

SBK and sbk-charts remain independently built and released projects.
sbk-analytics consumes them through a provider boundary:

```mermaid
flowchart TB
    subgraph Owners["Dependency project ownership"]
        SBKSource["SBK source"] --> SBKBuild["SBK build and tests"] --> SBKArtifact["Prebuilt release distribution"]
        ChartsSource["sbk-charts source"] --> ChartsBuild["Package and tests"] --> ChartsArtifact["Pinned release archive"]
    end

    subgraph Providers["Supported sbk-analytics providers"]
        Releases["Managed GitHub releases<br/>verified and cached"]
        Shared["Ready-to-run shared folders<br/>validated read-only"]
    end

    SBKArtifact --> Releases
    ChartsArtifact --> Releases
    SBKBuild -. "development output" .-> Shared
    ChartsBuild -. "development checkout/runtime" .-> Shared
    Releases --> Orchestrator["sbk-analytics orchestration lifecycle"]
    Shared --> Orchestrator
```

Mandatory Git submodules are intentionally excluded from the runtime design:

- **Ownership:** the orchestrator must not acquire responsibility for SBK's
  Gradle build or sbk-charts project packaging.
- **Reproducibility:** release versions, checksums, cache metadata, and source
  diagnostics identify what actually ran; mutable or dirty submodules weaken
  that guarantee.
- **Independent delivery:** each dependency can release, test, and patch on its
  own cadence without forcing an sbk-analytics source update.
- **Bootstrap stability:** cloning and running sbk-analytics must not require
  recursive submodule initialization or dependency build toolchains.
- **Operational simplicity:** validated caches support repeat and offline runs
  without rebuilding dependencies.

Shared-folder providers preserve the development workflow that submodules are
often proposed to solve. An SBK developer builds SBK using its owning project,
then points `sbk.local.folder` at that output. A charts developer points the
local-folder or explicit-executable setting at a ready command. Resolution,
execution, lifecycle handling, diagnostics, and reporting then follow the same
pipeline as managed releases.

An integration CI job may coordinate explicit checkouts and revisions, but it
must hand ready providers to sbk-analytics. It does not change this runtime
ownership boundary.

### 3. Execution Flow
```mermaid
flowchart LR
    Config["OrchestratorConfig"] --> Generator["yaml_gen.py"]
    Generator --> Jobs["Per-instance SBK YAML"]
    Jobs --> Runner["runner.py"]
    Runner --> Local["sbk-yal"]
    Runner --> Gem["sbk-gem-yal"]
    Local --> Results["RunResult collection"]
    Gem --> Results
    Results --> Filter{"Exit code 0 and<br/>non-empty CSV?"}
    Filter -->|Yes| CSV["Usable CSV inputs"]
    Filter -->|No| Diagnostics["Preserve logs and partial files<br/>Mark instance failed"]
    CSV --> Charts["charts.py → sbk-charts"] --> Excel["Excel report"]
```

### 4. Post-Processing Flow
```mermaid
flowchart LR
    Excel["sbk-charts Excel report"] --> Collector["system_info.py"]
    Local["Local host details"] --> Collector
    Remote["Configured remote-node details"] --> Collector
    Collector --> Sheet["System sheet"] --> Final["Final Excel output"]
```

### 5. Process Lifecycle Flow
```mermaid
flowchart TB
    CLI["cli.py signal context"] --> Workload["runner.py / charts.py"]
    Workload --> Managed["Managed process group"]
    Managed --> Normal{"How does execution end?"}

    Normal -->|Normal completion| Status["Trust native exit status<br/>Remove remaining descendants"]
    Normal -->|Catchable signal| Kind{"SBK-GEM?"}
    Kind -->|No| Term["TERM process group"] --> Grace["Standard grace"] --> Force{"Still running?"}
    Kind -->|Yes| GemTerm["TERM SBK-GEM group"] --> Native["Allow native remote cleanup<br/>30-second grace"] --> GemRunning{"Still running?"}
    GemRunning -->|No| Done["Cleanup complete"]
    GemRunning -->|Yes| Kill["KILL only owned local process group"]
    Force -->|No| Done
    Force -->|Yes| Kill

    ParentDeath["Uncatchable parent death"] --> EOF["Liveness pipe closes"]
    EOF --> Guard["Independent process guard"] --> GuardTerm["TERM, then KILL process group"]

    Register["Durable run record<br/>PID + start time + PGID + command"] --> Stale{"Controller still valid?"}
    Stale -->|Yes| Preserve["Preserve concurrent active run"]
    Stale -->|No| Verify{"Workload identity matches?"}
    Verify -->|Yes| Reconcile["TERM then KILL stale local group"]
    Verify -->|No| Quarantine["Quarantine record; do not signal"]
```

Every SBK and sbk-charts invocation is registered in memory and in a durable,
credential-free per-user ownership registry until its process tree exits.
Guard and registry creation are fail-closed. A later benchmark or `deps doctor`
reconciles stale records only after PID creation time, process group, and
command identity all match. Normal wrapper exit also triggers removal of any
remaining descendants in its workload group.

SBK-GEM owns normal deployment, benchmark timing, failure reporting, embedded
SBM, and remote cleanup. Catchable GEM interruptions allow native cleanup
first. Analytics never uses a global remote process-name kill because it cannot
distinguish concurrent runs; it records remote node names for diagnostics but
does not persist credentials.

## Key Design Decisions

### 1. JDK Resolution Priority
**Decision**: SBK_JAVA_HOME > JAVA_HOME > PATH > cached > download

**Rationale**: 
- Allows explicit JDK specification via SBK_JAVA_HOME
- Respects user's JAVA_HOME but doesn't override it
- Provides fallback to PATH and cached versions
- Auto-downloads as last resort

### 2. Environment Separation
**Decision**: Isolate application, sbk-charts, and JDK/SBK runtimes

**Rationale**:
- Prevents active venv/Conda environments from being modified
- Prevents sbk-charts dependency upgrades from changing sbk-analytics
- Makes the exact application runtime reusable offline after first bootstrap
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
- Verifies uv platform artifacts and the shipped sbk-charts source archive

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

```mermaid
flowchart LR
    subgraph Inputs
        ConfigYAML["Benchmark YAML"]
        PropertiesFile["sbk-config.env"]
    end

    subgraph RuntimeModels["Validated runtime models"]
        ConfigModel["OrchestratorConfig"]
        VersionsModel["Versions"]
    end

    subgraph BenchmarkData["Benchmark data"]
        GeneratedYAML["Generated SBK YAML"]
        SBKRun["SBK instances"]
        CSV["Successful CSV files"]
    end

    subgraph ReportData["Report data"]
        Charts["Performance charts and analysis"]
        System["System information"]
        Excel["Final Excel report"]
    end

    ConfigYAML --> ConfigModel --> GeneratedYAML --> SBKRun --> CSV --> Charts --> Excel
    PropertiesFile --> VersionsModel --> SBKRun
    System --> Excel
```

## Error Handling Strategy

### Graceful Degradation
- Failed SBK instances don't stop execution
- Only successful, non-empty CSV results are sent to sbk-charts
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
- SBK-native benchmark lifecycle and authoritative completion status
- Managed process-tree and parent-death cleanup
- Resource cleanup on exit
- Optional fail-closed pre-run workdir-content cleanup after dependency
  validation and before generated artifacts or workload processes

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
- SBK_JAVA_HOME set only in validated SBK child environments
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
- **Not supported**: Native Windows

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
