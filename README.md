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
| JDK       | ≥ matching SBK build (e.g. SBK 9.0 needs **JDK 25**) | The SBK release archive ships `.class` files compiled with a specific JDK; the orchestrator does not bundle Java. |
| `git`     | any        | Used by `pip` to install `sbk-charts` from its GitHub tag. |
| Internet access | yes  | First run downloads the SBK release tar from GitHub and pip-installs `sbk-charts`. Subsequent runs are offline. |

If your network intercepts TLS (corporate proxy with a custom root CA),
set `ssl.verify=false` in your `sbk-config.env` file:

```ini
# sbk-config.env
ssl.verify=false
```

This disables SSL verification for:
- GitHub API calls (release metadata)
- SBK/JDK downloads via requests
- pip git-based installations of sbk-charts

**Warning:** This is less secure and should only be used in trusted networks or
development environments. For production environments, ensure your system
has the correct CA certificates installed.

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
- Creates a separate venv for sbk-charts under `{sbk.folder}/sbk-charts/{version}/venv`
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
- **Cache**: No separate venv for sbk-charts; uses conda environment
- **Folder Structure**:
  ```
  .sbk/
  ├── 10.0/              # SBK installation
  └── sbk-charts/        # sbk-charts cache (metadata only, not venv)
      └── <version>/     # Version from sbk-config.env
          └── .ok       # Installation marker
  ```

#### Venv Environment Detection
- **Detection**: Assumes venv when `CONDA_PREFIX` is not set
- **Behavior**: Creates isolated venv for sbk-charts under `{sbk.folder}/sbk-charts/{version}/venv`
- **Benefits**: Complete isolation from system Python
- **Cache**: Caches sbk-charts venv for faster subsequent runs
- **Folder Structure**:
  ```
  .sbk/
  ├── 10.0/              # SBK installation
  └── sbk-charts/        # sbk-charts cache with full venv
      └── <version>/     # Version from sbk-config.env
          ├── .ok       # Installation marker
          └── venv/     # Isolated Python environment
              ├── bin/
              └── lib/
  ```

#### Manual Override

If you need to force a specific behavior, you can:
- **Force venv mode in conda**: Unset `CONDA_PREFIX` temporarily
- **Force conda mode in venv**: Set `CONDA_PREFIX` to your environment path

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

1. **Resolve versions.** Reads `<repo>/sbk-config.env` for the SBK and
   sbk-charts release tags.
2. **Download / install (once).**
   - Downloads the SBK release archive from GitHub and extracts it to
     `~/.cache/sbk-analytics/sbk/<sbk.version>/extracted/`. Cached on
     subsequent runs.
   - Installs `sbk-charts` of the configured tag into a private venv at
     `~/.cache/sbk-analytics/sbk-charts/<sbk-charts.version>/venv/`. Cached
     on subsequent runs.
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

# 2. Make sure JAVA_HOME points at a JDK new enough for the SBK build:
#    SBK 9.0 needs JDK 25
export JAVA_HOME=/opt/jdk-25
export PATH=$JAVA_HOME/bin:$PATH

# 3. Run a 120 s single-writer benchmark on `file` and `rocksdb`
sbk-analytics -c examples/file-rocksdb-write.yml -w /tmp/sbk-bench/work -v

# 4. Open the result
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
sbk.folder=./.sbk
sbk.jdk.version=25
sbk.jdk.folder=./.jdk
ssl.verify=true

sbk-charts.url=https://github.com/kmgowda/sbk-charts
sbk-charts.version=4.26.6.2
```

Recognised keys (case-insensitive; dots / underscores / dashes interchangeable):

| Key | Required | Notes |
| --- | --- | --- |
| `sbk.url`        | no (defaults to `https://github.com/kmgowda/SBK`)        | Full URL `https://github.com/<owner>/<repo>` or `<owner>/<repo>` shorthand. |
| `sbk.version`    | yes | Tag that exists on that repository's Releases page. |
| `sbk.folder`     | no (defaults to `./.sbk`) | Local folder for SBK and sbk-charts installation. |
| `sbk.jdk.version`| no (defaults to `25`) | Required JDK major version. |
| `sbk.jdk.folder`| no (defaults to `./.jdk`) | Local folder for JDK installation. |
| `ssl.verify`     | no (defaults to `true`) | Enable SSL verification for downloads. |
| `sbk-charts.url` | no (defaults to `https://github.com/kmgowda/sbk-charts`) | Same format as `sbk.url`. |
| `sbk-charts.version` | yes | Tag on the sbk-charts repository. |

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
| `-c`, `--config`     | path to the input YML (required) |
| `-p`, `--properties` | path to the SBK config `.env` file (optional; defaults to bundled `<project>/sbk-config.env`) |
| `-w`, `--work-dir`   | working dir for generated YAMLs / CSVs / logs / Excel report. Precedence: this flag > the YAML's `workdir:` > `/tmp/sbk-analytics`. |
| `-v`, `--verbose`    | repeat for more verbose logging (`-v` info, `-vv` debug) |
| `-h`, `--help`       | show help and exit |

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
| other | `sbk-charts` exit code |

## Caching

Release artifacts are cached under `~/.cache/sbk-analytics/` (override with the
`SBK_ANALYTICS_CACHE` environment variable). Set `GITHUB_TOKEN` to avoid
unauthenticated rate limits when first downloading.

Layout per version:

```
~/.cache/sbk-analytics/
├── sbk/<sbk.version>/extracted/sbk-<version>/   # SBK install (bin/, lib/)
└── sbk-charts/<sbk-charts.version>/venv/        # sbk-charts venv
```

The original SBK tarball is removed after extraction. Re-runs of the same
versions hit the cache and skip the download + install entirely.

## Troubleshooting

- **`UnsupportedClassVersionError: ... class file version 69.0 ...`** — your
  JDK is older than what the SBK release expects. SBK 9.0 needs JDK 25; install
  Temurin 25 and point `JAVA_HOME` at it.
- **`SSL: CERTIFICATE_VERIFY_FAILED ...`** — TLS interception by a corporate
  proxy. Export `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `PIP_CERT`, and
  `GIT_SSL_CAINFO` to the local CA bundle (see [Prerequisites](#prerequisites)).
- **An SBK instance hangs after printing the `Total ...` line** — known
  upstream issue (RocksDB driver and a few others don't release JVM threads
  on shutdown). `sbk-analytics` kills the process at `seconds + 5` and uses
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
    ├── charts.py             # single sbk-charts invocation
    └── system_info.py        # appends `system` sheet to the final xlsx
```
