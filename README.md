# sbk-analytics

Performance benchmarking with [SBK](https://github.com/kmgowda/SBK) and analytics
with [sbk-charts](https://github.com/kmgowda/sbk-charts), combined into a single
orchestrator.

`sbk-analytics` reads two inputs:

1. A **SBK configuration file** that pins the release tags of SBK and
   sbk-charts to use, along with folder paths and SSL settings. The corresponding
   release assets are downloaded once and cached under the specified folders
   (default: ./.sbk for SBK/sbk-charts, ./.jdk for JDK).
2. A **YML configuration file** describing the benchmark run.

From those it:

- Generates one YAML config per storage class for `sbk-yal` (or `sbk-gem-yal`
  if the `sbk.nodes` parameter is present).
- Runs an SBK instance per class, in serial or parallel mode, each emitting a
  CSV via `CSVLogger`.
- Invokes `sbk-charts` **once** at the end with all successful CSVs to produce
  a single Excel file with comparison charts (and optional AI-generated
  analytics).
- Appends a `system` sheet to that xlsx with CPU, memory, and disk details of
  the host.

If **all** SBK instances fail, `sbk-charts` is **not** executed.

## Local packages and dependency diagnostics

Use already-built packages without any SBK or sbk-charts download:

```bash
sbk-analytics config init --output sbk-config.local.env
# Edit the two local folder paths, then validate everything:
sbk-analytics deps doctor -p sbk-config.local.env
sbk-analytics -p sbk-config.local.env -c examples/file-rocksdb-write-60s.yml
```

For one-off runs, CLI paths are more convenient:

```bash
sbk-analytics --sbk-local /root/projects/SBK \
  --sbk-charts-local /root/projects/sbk-charts \
  -c examples/file-rocksdb-write-60s.yml
```

The resolver prints `LOCAL`, `MANAGED_CACHE`, `DOWNLOADED`, or `CONDA` plus
the exact executable selected. An explicitly selected invalid local folder is
an error and never silently falls back to the network. `sbk-charts` is resolved
only after a benchmark produces usable CSV input; use `deps doctor` to check it
before a long run.

## Quick Start

### For macOS/Linux (Recommended with Conda)
```bash
conda env create -f environment.yml
conda activate sbk-analytics
pip install -e .
sbk-analytics -c examples/file-rocksdb-write-60s.yml
```

### For Windows/Standard Python
```bash
python3 -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
sbk-analytics -c examples/file-rocksdb-write-60s.yml
```

## AI Agent Documentation

**For AI coding assistants**: See [analytics/AGENTS.md](analytics/AGENTS.md) for comprehensive documentation including:

- **Project Structure**: Detailed file-by-file breakdown
- **Architecture Overview**: Component interactions and data flow
- **Development Workflow**: How to make changes and test them
- **Key Design Decisions**: Important architectural choices
- **YAML Configuration Generation**: Complete guide for generating workload YAML files, including:
  - YAML schema and parameter reference
  - All storage driver classes and their parameters
  - Common workload patterns with examples
  - Best practices for YAML generation
  - Validation rules and troubleshooting
- **Troubleshooting Guide**: Common issues and solutions
- **Release Process**: How to build and distribute packages

### Quick Reference for AI Agents
- **Main package**: `analytics/` (Python package)
- **Entry point**: `analytics/cli.py:main()`
- **Configuration**: `sbk-config.env` (SBK versions, URLs, folders)
- **Examples**: `examples/` directory
- **Dependencies**: `requirements.txt` and `pyproject.toml`
- **Key modules**: `cli.py`, `releases.py`, `runner.py`, `charts.py`, `yaml_gen.py`

## Additional Documentation

- **[analytics/ARCHITECTURE.md](analytics/ARCHITECTURE.md)** - High-level architecture and design
- **[analytics/CONTRIBUTING.md](analytics/CONTRIBUTING.md)** - Guidelines for contributors
- **[analytics/DEVELOPMENT.md](analytics/DEVELOPMENT.md)** - Quick development reference
- **[analytics/SUPPORT.md](analytics/SUPPORT.md)** - Help and troubleshooting guide
- **[analytics/CHANGELOG.md](analytics/CHANGELOG.md)** - Version history and changes
- **[analytics/AGENTS.md](analytics/AGENTS.md)** - Comprehensive AI agent documentation

## File Path Configuration

### Work Directory

The `workdir` parameter in your YAML configuration specifies where all output files are stored:
- Generated SBK YAML files (`yml/`)
- Per-instance CSV files (`csv/`)
- Parallel-mode logs (`logs/`)
- Final Excel report (when `sbk-charts.output` is a bare filename)

The workdir is automatically created by sbk-analytics. Default: `/tmp/sbk-analytics`

### Storage Driver File Paths

When configuring storage drivers that require file paths (like `file`, `rocksdb`, etc.), ensure the parent directories exist. Two approaches:

#### Option 1: Use the Work Directory (Recommended)
```yaml
workdir: /tmp/sbk-analytics

classes:
  - class: file
    file: /tmp/sbk-analytics/file-60s.dat  # Uses workdir
  - class: rocksdb
    rfile: /tmp/sbk-analytics/rocksdb-60s   # Uses workdir
```

#### Option 2: Create Custom Directories
```yaml
workdir: /tmp/sbk-analytics

classes:
  - class: file
    file: /custom/path/file-60s.dat  # Ensure /custom/path exists
  - class: rocksdb
    rfile: /custom/path/rocksdb-60s   # Ensure /custom/path exists
```

### Important Notes

- **Workdir is auto-created**: sbk-analytics automatically creates the workdir
- **Custom paths must exist**: If you use paths outside the workdir, create them manually
- **Relative paths**: File paths can be relative to the current directory
- **Absolute paths**: Recommended for reproducibility

## Prerequisites

`sbk-analytics` itself is a small Python package, but the tools it
orchestrates need a working runtime on the host:

| Tool | Required version | Notes |
| --- | --- | --- |
| Python    | ≥ 3.9      | Tested with 3.10 / 3.12. |
| JDK       | ≥ matching SBK build (e.g. SBK 10.0 needs **JDK 25**) | Automatically resolved and cached by default. The SBK release archive ships `.class` files compiled with a specific JDK. |
| `git`     | any        | Used by `pip` when installing a configured remote sbk-charts tag. Not needed for a ready-to-run local sbk-charts checkout. |
| Internet access | conditional | Needed for dependencies that are neither configured locally nor already cached. |

TLS verification defaults to `false`, as shown in the bundled configuration:

```ini
# sbk-config.env
ssl.verify=false
```

This disables SSL verification for:
- GitHub API calls (release metadata)
- SBK/JDK downloads via requests
- pip git-based installations of sbk-charts

For stricter environments, set `ssl.verify=true`. A private trust root can be
selected with `ssl.ca.bundle=/path/to/company-ca.pem`. Invalid boolean values
are rejected instead of being treated as false.

## Build / install

### Option 1: Conda Installation (Recommended for macOS/Linux)

Conda is recommended for macOS and Linux as it handles platform-specific dependencies (like PyTorch) better than pip.

#### Step-by-step Conda Installation

```bash
# 1. Clone the repository
git clone <this-repo-url> sbk-analytics
cd sbk-analytics

# 2. Create conda environment from environment.yml
conda env create -f environment.yml

# 3. Activate the environment
conda activate sbk-analytics

# 4. Install sbk-analytics in development mode
pip install -e .

# 5. Verify installation
sbk-analytics --version
```

#### What the Conda Installation Does

The `environment.yml` file includes:
- **Python 3.10** - Runtime environment
- **PyTorch** - Platform-appropriate version (handles Apple Silicon vs Intel automatically)
- **Core dependencies** - pyyaml, requests, etc.
- **Note**: sbk-charts is installed by sbk-analytics based on `sbk-config.env`, not by conda directly. This ensures version consistency with the configuration file.

#### Conda Environment Behavior

When using conda, sbk-analytics automatically detects the conda environment and:
- Installs sbk-charts directly in the conda environment (not a separate venv)
- Uses conda's PyTorch package for better platform compatibility
- Respects SSL verification settings from `sbk-config.env`
- Checks for existing installations before attempting to install

#### Updating Conda Environment

```bash
# Update the environment if you modify environment.yml
conda env update -f environment.yml --prune

# Reinstall sbk-analytics if you make code changes
pip install -e .
```

### Option 2: Virtual Environment Installation

For users who prefer standard Python virtual environments.

#### Step-by-step Venv Installation

```bash
# 1. Clone the repository
git clone <this-repo-url> sbk-analytics
cd sbk-analytics

# 2. Create virtual environment
python3 -m venv .venv

# 3. Activate the environment
# On Linux/macOS:
. .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# 4. Upgrade pip
python -m pip install --upgrade pip

# 5. Install dependencies
python -m pip install -r requirements.txt

# 6. Install sbk-analytics in development mode
python -m pip install -e .

# 7. Verify installation
sbk-analytics --version
```

#### Venv Environment Behavior

When using venv, sbk-analytics:
- Creates a separate venv for sbk-charts under `{downloads.folder}/sbk-charts/{version}/venv`
- Downloads and installs all dependencies including PyTorch via pip
- Respects SSL verification settings from `sbk-config.env`
- Caches installations for faster subsequent runs

### macOS Installation Notes

#### Platform-Specific Dependencies

macOS users may encounter issues with platform-specific dependencies like PyTorch,
especially on Apple Silicon (M1/M2/M3) vs Intel architectures. Common issues include:

- `torch~=2.9.1` not available for your specific macOS/Python combination
- Missing pre-built wheels for your architecture
- Binary compatibility issues

#### Recommended Solution: Use Conda

**For macOS users, the conda installation method (Option 1) is strongly recommended**
as it handles these platform-specific dependencies automatically through the pytorch channel.

#### Alternative Solutions for macOS

If you prefer not to use conda on macOS:

1. **Use an older sbk-charts version** with more compatible dependencies:
   ```ini
   # sbk-config.env
   sbk-charts.version=4.26.0.1
   ```

2. **Install PyTorch separately with conda, then use pip for sbk-charts**:
   ```bash
   conda install pytorch
   pip install git+https://github.com/kmgowda/sbk-charts.git@<version-from-sbk-config.env>
   ```

3. **Build PyTorch from source** (time-consuming but guaranteed to work):
   ```bash
   pip install torch==2.9.1 --no-binary :all:
   ```

### Verification

After installation, verify that everything works:

```bash
# Check version
sbk-analytics --version

# Run a simple benchmark (this will download dependencies on first run)
sbk-analytics -c examples/file-rocksdb-write-60s.yml
```

### Troubleshooting

#### Venv Installation Issues

If you encounter PyTorch installation issues with venv:
- Switch to conda installation (Option 1)
- Use an older sbk-charts version
- Install PyTorch separately before sbk-charts

#### Conda Installation Issues

If conda installation fails:
- Ensure you have conda or miniconda installed
- Update conda: `conda update conda`
- Try mamba instead of conda for faster dependency resolution
- Check that you have write permissions for the conda environment directory

#### SSL Certificate Issues

If you encounter SSL certificate errors:
- Set `ssl.verify=false` in your `sbk-config.env` file
- This is useful for corporate proxies with self-signed certificates
- See the SSL verification section below for details

### Environment Detection

sbk-analytics automatically detects whether you're using conda or venv and adjusts its behavior accordingly:

#### Conda Environment Detection
- **Detection**: Checks for `CONDA_PREFIX` environment variable
- **Behavior**: Installs sbk-charts directly in the conda environment
- **Benefits**: Uses conda's PyTorch package for better platform compatibility
- **Cache**: No separate venv for sbk-charts; uses conda environment (no caching)
- **Folder Structure** (default project-local):
  ```
  ./.sbk/
  ├── <sbk.version>/         # SBK installation
  │   ├── .ok              # Installation marker
  │   ├── .home            # Path to extracted SBK home
  │   └── extracted/       # Extracted SBK distribution
  └── sbk-charts/          # sbk-charts metadata (no venv in conda mode)
  
  ./.jdk/
  └── <jdk.version>/        # JDK installation
      ├── .ok              # Installation marker
      ├── .home            # Path to JDK home
      └── extracted/       # Extracted JDK distribution
  ```

#### Venv Environment Detection
- **Detection**: Assumes venv when `CONDA_PREFIX` is not set
- **Behavior**: Creates isolated venv for sbk-charts under project folder
- **Benefits**: Complete isolation from system Python
- **Cache**: Caches sbk-charts venv for faster subsequent runs
- **Folder Structure** (default project-local):
  ```
  ./.sbk/
  ├── <sbk.version>/         # SBK installation
  │   ├── .ok              # Installation marker
  │   ├── .home            # Path to extracted SBK home
  │   └── extracted/       # Extracted SBK distribution
  └── sbk-charts/
      └── <sbk-charts.version>/ # sbk-charts cache with full venv
          ├── .ok              # Installation marker
          └── venv/            # Isolated Python environment
              ├── bin/
              └── lib/
  
  ./.jdk/
  └── <jdk.version>/        # JDK installation
      ├── .ok              # Installation marker
      ├── .home            # Path to JDK home
      └── extracted/       # Extracted JDK distribution
  ```

#### Manual Override

If you need to force a specific behavior, you can:
- **Force venv mode in conda**: Unset `CONDA_PREFIX` temporarily
- **Force conda mode in venv**: Set `CONDA_PREFIX` to your environment path

## macOS Logging Issues

If you experience missing SBK logs on macOS, this is due to Java output buffering. The application has been configured to automatically handle this on macOS, but if you still encounter issues:

### Solutions

1. **Use verbose mode** to ensure all Python logs are visible:
   ```bash
   sbk-analytics -c examples/file-rocksdb-write-60s.yml -v
   ```

2. **Force log forwarding** (useful on some macOS terminals):
   ```bash
   sbk-analytics -c examples/file-rocksdb-write-60s.yml --forward-logs
   ```

3. **Combine both options** for maximum visibility:
   ```bash
   sbk-analytics -c examples/file-rocksdb-write-60s.yml -v --forward-logs
   ```

4. **Check terminal buffering**: Some macOS terminals may buffer output. Try:
   ```bash
   script -q /dev/null sbk-analytics -c examples/file-rocksdb-write-60s.yml
   ```

5. **Use recommended terminals**: iTerm2 or Terminal.app have better output handling than some third-party terminals.

### Technical Details

The application now:
- **Automatic macOS detection**: On macOS, SBK logs are captured and forwarded in real-time using a separate thread
- **Java unbuffering**: Sets `JAVA_TOOL_OPTIONS` to disable Java output buffering
- **Manual override**: The `--forward-logs` flag forces real-time log forwarding on any platform
- **Line buffering**: Uses line-buffered subprocess output to ensure logs appear immediately

### Why SBK Logs Were Missing

Java applications (like SBK) buffer stdout/stderr by default. On macOS, this buffering is more aggressive, causing logs to appear only after the process completes or not at all. The fix explicitly captures Java output and forwards it line-by-line to ensure real-time visibility.

## Download Progress

During first-run downloads (JDK, SBK, sbk-charts), sbk-analytics displays
real-time download progress showing:
- Percentage complete
- Downloaded vs total size (in MB)
- Current download speed (in MB/s)

Progress updates every 2 seconds so you can monitor download activity.

## Build / install

### Option 1: Conda Installation (Recommended for macOS/Linux)

Conda is recommended for macOS and Linux as it handles platform-specific dependencies (like PyTorch) better than pip:

```bash
# Create conda environment from environment.yml
conda env create -f environment.yml

# Activate the environment
conda activate sbk-analytics

# Install sbk-analytics in development mode
pip install -e .
```

The `environment.yml` file includes:
- Python 3.10
- PyTorch (with platform-appropriate version)
- sbk-charts (installed from GitHub)
- All other required dependencies

### Option 2: Virtual Environment Installation

Clone the repository (or unpack the source tree), then create a virtual
environment and install in editable mode:

```bash
git clone <this-repo-url> sbk-analytics
cd sbk-analytics

python3 -m venv .venv
. .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

This installs the package and exposes the `sbk-analytics` command on `PATH`.
The bundled `sbk-config.env` at the repo root is found automatically.

Verify the install:

```bash
sbk-analytics --help
```

## Run

The minimum required arguments are `-c <config.yml>`:

```bash
sbk-analytics -c examples/file-rocksdb-write.yml -w ./run-1 -v
```

What this does, step by step:

1. **Resolve dependencies.** Reads `<repo>/sbk-config.env`. Explicit
   `sbk.local.folder` and `sbk-charts.local.folder` values take priority;
   otherwise the configured release tags are resolved from cache or GitHub.
2. **Use local or download / install (once).**
   - Validates ready-to-run local folders without modifying them. An invalid
     explicitly configured local folder fails immediately and never falls back
     to a remote package.
   - Downloads the JDK (if needed) from Temurin and extracts it to
     `./.jdk/<jdk.version>/extracted/` (or cache if configured differently). Cached on subsequent runs.
   - Downloads the SBK release archive from GitHub and extracts it to
     `./.sbk/<sbk.version>/extracted/` (or cache if configured differently). Cached on subsequent runs.
   - Installs `sbk-charts` of the configured tag (in conda environment or
     isolated venv at `./.sbk/sbk-charts/<sbk-charts.version>/venv/`).
     Cached on subsequent runs (venv mode only).
3. **Generate per-instance YAMLs.** One YAML per `classes:` entry under
   `<work-dir>/yml/`, each forced to write CSV via `CSVLogger` to a unique
   `<work-dir>/csv/sbk-<instance>.csv`.
4. **Run SBK.** Invokes `sbk-yal` (or `sbk-gem-yal` if any instance has
   `nodes:`) once per instance. In `serial` mode (default) SBK output is
   shown live; in `parallel` mode each instance writes to its own log file
   and the orchestrator prints a heartbeat every 5 seconds. If any instance
   exceeds `seconds + 5`, it is killed forcefully and the partial CSV is
   still used.
5. **Run sbk-charts once.** Feeds every produced CSV into a single
   `sbk-charts` invocation, with the output xlsx, AI backend, and `-chat`
   flag taken from the YAML's `sbk-charts:` group.
6. **Append the `system` sheet** to the xlsx (CPU, RAM, disks).

### Full end-to-end example

```bash
# 1. Activate the venv created during "Build / install"
. .venv/bin/activate

# 2. Run a 120 s single-writer benchmark on `file` and `rocksdb`
#    JDK is automatically resolved and cached
sbk-analytics -c examples/file-rocksdb-write.yml -w /tmp/sbk-bench/work -v

# 3. Open the result
ls /tmp/sbk-bench/sbk-analytics.xlsx
```

After the run:

```
/tmp/sbk-bench/sbk-analytics.xlsx       # final report (comparison + system sheet)
/tmp/sbk-bench/work/yml/sbk-file.yml    # generated SBK YAML for the `file` instance
/tmp/sbk-bench/work/yml/sbk-rocksdb.yml # generated SBK YAML for the `rocksdb` instance
/tmp/sbk-bench/work/csv/sbk-file.csv    # raw CSV from the `file` instance
/tmp/sbk-bench/work/csv/sbk-rocksdb.csv # raw CSV from the `rocksdb` instance
```

## Inputs

### 1. Versions properties file (`.env` style)

A default `sbk-config.env` ships with the project at the repository root and
carries both the **GitHub URL** and the **release tag** for each project:

```ini
# sbk-config.env  (bundled at the project root)
sbk.url=https://github.com/kmgowda/SBK
sbk.version=10.0
# sbk.local.folder=/root/projects/SBK
downloads.folder=./.sbk
sbk.jdk.version=25
sbk.jdk.folder=./.jdk
ssl.verify=true

sbk-charts.url=https://github.com/kmgowda/sbk-charts
sbk-charts.version=4.26.6.2
# sbk-charts.local.folder=/root/projects/sbk-charts
```

### JDK Resolution Order

The orchestrator resolves a JDK whose major version matches `sbk.jdk.version`
by probing in the following order:

1. **SBK_JAVA_HOME** (exported by the user) - highest priority
   - If set and points to the required version, use it
   - If set but wrong version, proceed to next step

2. **JAVA_HOME** (exported by the user)
   - If set and points to the required version, use it
   - If set but wrong version, proceed to next step
   - When used, sets SBK_JAVA_HOME to this location
   - Note: JAVA_HOME is explicitly unset in subprocess to prevent SBK from using wrong Java version

3. **java on PATH**
   - If it reports the required version, use it
   - If wrong version, proceed to next step
   - When used, sets SBK_JAVA_HOME to the JDK home location
   - Note: JAVA_HOME is explicitly unset in subprocess to prevent SBK from using wrong Java version

4. **Specified jdk folder** (if `sbk.jdk.folder` is set in sbk-config.env)
   - Check if cached version matches required version
   - If match, use it; otherwise proceed to download
   - When used, sets SBK_JAVA_HOME to the JDK home location
   - Note: JAVA_HOME is explicitly unset in subprocess to prevent SBK from using wrong Java version

5. **Download Temurin** to specified folder or cache
   - Download Temurin of the required major version from Adoptium API
   - Extract to the specified folder (default: `./.jdk/<version>/extracted/`) or cache location
   - Set SBK_JAVA_HOME to point to the downloaded JDK
   - Note: JAVA_HOME is explicitly unset in subprocess to prevent SBK from using wrong Java version
   - Cache for future builds

Recognised keys (case-insensitive; dots / underscores / dashes interchangeable):

| Key | Required | Notes |
| --- | --- | --- |
| `sbk.url`        | no (defaults to `https://github.com/kmgowda/SBK`)        | Full URL `https://github.com/<owner>/<repo>` or `<owner>/<repo>` shorthand. |
| `sbk.version`    | yes | Tag that exists on that repository's Releases page. |
| `sbk.local.folder` | no | Ready-to-run SBK distribution or built source checkout. Takes priority over cache and URL. |
| `downloads.folder` | no (defaults to `./.sbk`) | Shared local folder for downloaded SBK and sbk-charts installations. Use `./.sbk` for a project-local cache. |
| `sbk.jdk.version`| no (defaults to `25`) | Required JDK major version. |
| `sbk.jdk.folder`| no (defaults to `./.jdk`) | Local folder for JDK installation. Use `./.jdk` for project-local cache. |
| `ssl.verify`     | no (defaults to `true`) | Enable SSL verification for downloads. |
| `sbk-charts.url` | no (defaults to `https://github.com/kmgowda/sbk-charts`) | Same format as `sbk.url`. |
| `sbk-charts.version` | yes | Tag on the sbk-charts repository. |
| `sbk-charts.local.folder` | no | Ready-to-run sbk-charts checkout or environment. Takes priority over conda, cache, and URL. |

### Using local SBK and sbk-charts

Use the two local-folder settings independently or together:

```ini
sbk.local.folder=/root/projects/SBK
sbk-charts.local.folder=/root/projects/sbk-charts
```

An SBK distribution root must contain `bin/sbk-yal`; GEM workloads also
require `bin/sbk-gem-yal`. A built SBK source checkout is accepted when the
same commands are under `build/install/sbk/bin/` (for example, after Gradle
`installDist`). A local sbk-charts folder must contain either
`sbk-charts` at its root or `bin/sbk-charts`.

Local folders are authoritative and read-only from sbk-analytics' perspective:
it does not create `.ok`/`.home`, change permissions, install dependencies, or
fall back to GitHub when validation fails. Relative paths are resolved against
the directory containing `sbk-config.env`.

Every run prints `LOCAL`, `MANAGED_CACHE`, `DOWNLOADED`, or `CONDA`, together
with the exact selected folder and executable. The configured remote version
is informational and ignored for a local checkout.

You don't need to pass `-p` / `--properties` — `sbk-analytics` automatically
uses the bundled file. Pass `-p <path>` only if you want to override it
(e.g. to benchmark a fork of SBK):

```bash
sbk-analytics -c my-run.yml -p /path/to/custom-sbk-config.env
```

```ini
# my-fork-sbk-config.env
sbk.url=https://github.com/your-org/SBK
sbk.version=9.0-myfork
sbk-charts.url=kmgowda/sbk-charts
sbk-charts.version=3.26.2.1
```

```bash
sbk-analytics -p my-fork.env -c config.yml
```

### 2. Input YML

```yaml
# examples/config.yml
mode: serial            # 1. serial | parallel (default: serial)

workdir: /tmp/sbk-analytics    # 1b. (optional) output dir for generated
                               #     YAMLs, CSVs and the Excel report.
                               #     Default: /tmp/sbk-analytics.

sbk:                    # 2. SBK-YAL / SBK-GEM-YAL defaults shared by every
  seconds: 60           #    instance (presence of 'nodes' switches to sbk-gem-yal)
  time: ms

classes:                # 3. one entry per benchmark instance
  - class: file
    writers: 1
    size: 100
    file: /tmp/sbk-bench-file
  - class: file         #    same class can appear multiple times with
    readers: 1          #    different params (e.g. read vs write, sizes, ...)
    size: 100
    file: /tmp/sbk-bench-file
  - class: hdfs
    writers: 1
    uri: hdfs://localhost:9000
    fname: /tmp/sbk-bench-hdfs

# class_params is optional and can be omitted entirely. If you don't need
# class-level defaults, leave it out -- every instance lists exactly the
# parameters that differ from the shared `sbk:` block.

sbk-charts:                    # 5. options for the sbk-charts invocation.
  output: sbk-analytics.xlsx   #    `output`: xlsx file (bare name -> workdir)
  ai_model: noai               #    `ai_model`: huggingface|ollama|lmstudio|noai
  ai_params: {}                #    `ai_params`: --key value pairs for AI plugin
  chat: false                  #    `chat`: enable sbk-charts -chat mode
  use_files:                   #    `use_files`: pre-existing CSVs (optional)
    - baseline-kafka.csv       #      paths resolve against workdir; absolute
    - /data/last-run.csv       #      paths are honoured verbatim
```

The CSV inputs to sbk-charts are always:

1. **one CSV per SBK instance** declared under `classes:` (auto-generated by
   sbk-analytics during the run; you do not set these), plus
2. **every CSV listed under `sbk-charts.use_files:`** (an optional list of
   pre-existing CSV files from earlier runs / other tools that you want to
   chart side-by-side with the fresh ones).

`use_files` paths may be absolute or relative to `workdir`. Missing or empty
entries are skipped with a warning. If **all** SBK instances fail **and**
`use_files` is empty, sbk-charts is skipped (exit code 2) — otherwise it
runs with whatever CSVs are available.

#### `classes` — multiple instances per class

Each entry under `classes:` becomes its own SBK invocation, producing its own
intermediate YAML and CSV. You can list the **same class multiple times** with
different parameters — e.g. one writer-only instance, one reader-only
instance, and a third writer instance with a larger record size:

```yaml
classes:
  - class: file
    writers: 1
    size: 100
  - class: file
    readers: 1
    size: 100
  - class: file
    writers: 1
    size: 1000
    name: file_big_writes    # optional explicit label for the CSV/YAML name
```

Without an explicit `name:`, instances are auto-labelled `<class>`,
`<class>-2`, `<class>-3`, ... so they always get unique YAML/CSV filenames.

Style A (legacy short form) is still supported and may be mixed with Style B:

```yaml
classes: [file, hdfs]
class_params:
  file: {fname: /tmp/sbk-test, writers: 1}
  hdfs: {uri: hdfs://localhost:9000, writers: 1}
```

Parameter precedence for the generated per-instance YAML (lowest to highest):

1. shared `sbk:` block (defaults for every instance)
2. `class_params[<class>]` (per-class defaults, if any)
3. the entry's own keys (only for Style B mapping entries)
4. the orchestrator's own overrides: `class`, `out: CSVLogger`, `csvfile`

Each instance can mix freely between **SBK parameters** (`writers`, `readers`,
`size`, `seconds`, `time`, ...) and **class-specific parameters** (`file`
for the File driver, `rfile` for RocksDB, `uri` for HDFS, brokers for Kafka,
etc.). Any SBK parameter the instance does **not** specify is inherited from
the shared `sbk:` block.

You do **not** set `class`, `out`, or `csvfile` yourself — they are managed
by `sbk-analytics`.

### Hard timeout

Hard kill timing is staged. All times are measured from the moment each
instance starts and use `seconds` as resolved for that instance's YAML.

| Stage | When (gem mode) | When (yal mode) | Action |
| --- | --- | --- | --- |
| 1. Remote kill | `seconds + 10` | n/a | SSH into every node in `nodes:` and run `pkill -9 -f io.sbk.main` so the remote sbk clients are killed first. |
| 2. Local kill  | `seconds + 15` | `seconds + 15` | SIGTERM then SIGKILL the local `sbk-yal` / `sbk-gem-yal` process. |

In gem mode the remote kill runs in a background thread so it can finish
during the 5-second window between stage 1 and stage 2; the local kill then
joins the remote-kill thread (up to 5 s) before terminating the local
process, so the remote sbk clients are gone before the local sbk-gem-yal is
torn down.

Remote-kill credentials come from the instance's `gemuser` / `gempass` /
`gemport` parameters. If `gempass:` is set, `sshpass` is preferred (and used
if present on PATH); otherwise key-based SSH is attempted. Failures (e.g.
unreachable nodes) are logged but do **not** fail the overall run.

Whatever CSV the instance had written up to the kill is preserved and fed
into the single `sbk-charts` invocation at the end.

### Interruption and forced-exit cleanup

`sbk-yal`, `sbk-gem-yal`, and `sbk-charts` run in isolated process trees. If
`sbk-analytics` receives Ctrl-C, SIGTERM, SIGHUP, SIGQUIT, or Windows SIGBREAK,
it asks every active tree to stop, waits up to 3 seconds, and then force-kills
anything that remains. This applies in serial and parallel modes and includes
shells, JVMs, and other descendants created by the launched command.

Abrupt parent death is covered too: POSIX platforms use an independent
parent-liveness guard, while Windows uses a kill-on-close Job Object. Thus an
uncatchable parent kill does not leave local SBK or sbk-charts descendants
running. For `sbk-gem-yal`, catchable interruptions also perform the existing
best-effort SSH cleanup of remote SBK clients. An uncatchable local kill cannot
run new SSH commands, so remote-host cleanup in that specific case depends on
the remote SBK/GEM connection lifecycle.

#### When the timeout does NOT apply

If the instance does **not** set `seconds:` (or sets it to `0` / a negative
value) — i.e. the benchmark is bounded by `records:` or runs forever —
**no timeout applies at all**. `sbk-analytics` will not kill the local
`sbk-yal` / `sbk-gem-yal` process, and will not run the remote `pkill`,
even if the run hangs indefinitely. This matches the intent of `records:`:
the benchmark stops when the configured number of records have been
written/read, not on a wall-clock deadline.

## CLI reference

| Flag | Meaning |
| --- | --- |
| `-c`, `--config`     | path to the input YML (required for a benchmark run) |
| `-p`, `--properties` | path to the SBK config `.env` file (optional; defaults to bundled `<project>/sbk-config.env`) |
| `-w`, `--work-dir`   | working dir for generated YAMLs / CSVs / logs / Excel report. Precedence: this flag > the YAML's `workdir:` > `/tmp/sbk-analytics`. |
| `-v`, `--verbose`    | repeat for more verbose logging (`-v` info, `-vv` debug) |
| `--forward-logs`    | force real-time SBK log forwarding (useful on macOS terminals) |
| `--sbk-local` | local SBK distribution or built checkout |
| `--sbk-charts-local` | local sbk-charts checkout/environment |
| `--sbk-charts-executable` | exact local sbk-charts command path |
| `--downloads-folder` | managed package cache; highest cache precedence |
| `--resolve-only` | resolve/validate dependencies without running SBK |
| `--json` | emit a machine-readable dependency/run summary |
| `-h`, `--help`       | show help and exit |

With `--json`, stdout contains exactly one JSON document on both handled
success and failure paths. Banners, progress messages, warnings, and child
process output are sent to stderr, so stdout can be safely piped to `jq` or
another JSON consumer.

### Modes

- **serial** (default): SBK instances run one at a time; stdout/stderr are
  inherited so you see the SBK output live.
- **parallel**: all SBK instances launch concurrently; per-instance output is
  redirected to `logs/sbk-<class>.log` under the working dir, and a heartbeat
  line is printed every 5 seconds with the still-running classes. A warning
  is emitted on stderr explaining the caveats.

### Outputs

After a successful run the working dir contains:

```
<workdir>/
├── yml/                       # generated per-instance SBK YAML files
├── csv/                       # CSV outputs from each SBK instance
├── logs/                      # per-instance log files (parallel mode only)
└── <sbk-charts.output>        # final Excel report (comparison charts + system sheet)
```

`<workdir>` is taken from (in order of precedence):

1. the `-w` / `--work-dir` CLI flag,
2. the input YAML's `workdir:` key (set right after `mode:`),
3. the default `/tmp/sbk-analytics`.

The Excel file ends up inside `<workdir>` when `sbk-charts.output` is a bare
filename (e.g. `sbk-analytics.xlsx`). If you want the report somewhere else,
set `sbk-charts.output` to a path containing a directory component (relative
or absolute) and that location is honoured verbatim.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success |
| 2 | all SBK instances failed AND no `use_files` were provided; `sbk-charts` was skipped |
| 3 | `sbk-charts` ran but did not produce the expected xlsx |
| 4 | failed to append the system sheet |
| 5 | configuration or dependency resolution failed |
| other | `sbk-charts` exit code |
| `128 + signal` | terminated by a catchable operating-system signal (for example, 130 for Ctrl-C and 143 for SIGTERM) |

## Caching

The cache precedence is `--downloads-folder`, an explicitly configured
`downloads.folder`, `SBK_ANALYTICS_DOWNLOADS_FOLDER` (or the legacy
`SBK_ANALYTICS_CACHE`), then `~/.cache/sbk-analytics`. The bundled file sets
`downloads.folder=./.sbk`, preserving the project-local default. Cache installs
are locked per version, completion is marked last, and `metadata.json` records
the requested version, source, executable, install time, and available SHA-256.

Local path precedence is CLI, environment (`SBK_LOCAL_FOLDER`,
`SBK_CHARTS_LOCAL_FOLDER`, `SBK_CHARTS_LOCAL_EXECUTABLE`), properties file,
then managed resolution.

Set top-level YAML `cleanup: on-success` to remove benchmark data after a
successful report. Cleanup currently supports only `class: file`, using its
`file` or legacy `fname` parameter. For safety, only paths inside `workdir` are
removed; RocksDB (`rfile`), other drivers, external paths, CSVs, logs, and the
Excel report are always preserved. The default is `cleanup: never`.
Set `GITHUB_TOKEN` to avoid unauthenticated rate limits when first downloading.

### Default Cache Structure (project-local)

By default, sbk-analytics uses project-local folders:

```
./.sbk/
├── <sbk.version>/             # SBK installation
│   ├── .ok                    # Installation marker
│   ├── .home                  # Path to extracted SBK home
│   └── extracted/             # Extracted SBK distribution
└── sbk-charts/
    └── <sbk-charts.version>/ # sbk-charts cache (venv mode only)
        ├── .ok                # Installation marker
        └── venv/              # Isolated Python environment

./.jdk/
└── <jdk.version>/             # JDK installation
    ├── .ok                    # Installation marker
    ├── .home                  # Path to JDK home
    └── extracted/             # Extracted JDK distribution
```

### System Cache Structure (when SBK_ANALYTICS_CACHE is set)

When `SBK_ANALYTICS_CACHE` is set or folders are explicitly set to non-default values:

```
~/.cache/sbk-analytics/ (or custom cache path)
├── jdk/<jdk.version>/              # JDK installation
│   ├── .ok                        # Installation marker
│   ├── .home                      # Path to JDK home
│   └── extracted/                 # Extracted JDK distribution
├── sbk/<sbk.version>/             # SBK installation
│   ├── .ok                        # Installation marker
│   ├── .home                      # Path to extracted SBK home
│   └── extracted/                 # Extracted SBK distribution
└── sbk-charts/<sbk-charts.version>/  # sbk-charts cache (venv mode only)
    ├── .ok                        # Installation marker
    └── venv/                      # Isolated Python environment
        ├── bin/
        └── lib/
```

**Note**: In conda environments, sbk-charts is installed directly in the conda environment and is not cached separately.

The original SBK tarball and JDK archive are removed after extraction. Re-runs of the same
versions hit the cache and skip the download + install entirely.

Local folders configured with `sbk.local.folder` or
`sbk-charts.local.folder` are outside this managed cache. sbk-analytics only
validates and invokes them; it never writes cache markers or installation data
into those folders.

## Troubleshooting

- **A configured local package is rejected** — confirm the folder has one of
  the supported ready-to-run layouts documented above and that its commands
  are executable. Explicit local folders fail fast and do not fall back to a
  download.

- **`UnsupportedClassVersionError: ... class file version 69.0 ...`** — your
  JDK is older than what the SBK release expects. SBK 10.0 needs JDK 25. The
  orchestrator automatically resolves and downloads the correct JDK version by
  default. If you need to use a specific JDK, set `SBK_JAVA_HOME` to point to it.
- **`SSL: CERTIFICATE_VERIFY_FAILED ...`** — TLS interception by a corporate
  proxy. Export `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `PIP_CERT`, and
  `GIT_SSL_CAINFO` to the local CA bundle (see [Prerequisites](#prerequisites)).
- **An SBK instance hangs after printing the `Total ...` line** — known
  upstream issue (RocksDB driver and a few others don't release JVM threads
  on shutdown). `sbk-analytics` kills the process at `seconds + 15` and uses
  the CSV that has already been flushed.
- **`sbk-charts` complains about a missing `banner.txt` or `images/sbk-logo.png`** —
  packaging quirks of some sbk-charts versions. `sbk-analytics` already supplies a
  stub `banner.txt` via a private cwd; the missing logo is harmless.
- **All SBK instances failed; exit code 2** — none of the configured SBK runs
  produced a non-empty CSV. `sbk-charts` is intentionally **not** invoked in
  this case. Check the per-instance YAMLs under `<work-dir>/yml/` and re-run.

## Project layout

```
sbk-analytics/
├── sbk-config.env            # bundled SBK / sbk-charts release pins
├── pyproject.toml            # entry point: sbk-analytics → analytics.cli:main
├── requirements.txt
├── README.md
├── examples/
│   ├── config.yml                  # generic multi-class example
│   └── file-rocksdb-write.yml      # 120s file + rocksdb single-writer example
└── analytics/
    ├── cli.py                # argument parsing + orchestration
    ├── properties.py         # sbk-config.env parser
    ├── config.py             # input YAML parser (sbk, classes, sbk-charts)
    ├── releases.py           # GitHub release download + cached install
    ├── yaml_gen.py           # per-instance sbkArgs/sbkGemArgs YAML generator
    ├── runner.py             # serial / parallel SBK execution + watchdog
    ├── processes.py          # managed workload trees + signal cleanup
    ├── _process_guard.py     # POSIX/Windows parent-death companion
    ├── charts.py             # single sbk-charts invocation
    └── system_info.py        # appends `system` sheet to the final xlsx
```
