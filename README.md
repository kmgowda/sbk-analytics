# sbk-analytics

Performance benchmarking with [SBK](https://github.com/kmgowda/SBK) and analytics
with [sbk-charts](https://github.com/kmgowda/sbk-charts), combined into a single
orchestrator.

`sbk-analytics` reads two inputs:

1. A **versions properties file** that pins the release tags of SBK and
   sbk-charts to use. The corresponding release assets are downloaded once and
   cached under `~/.cache/sbk-analytics/`.
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
set `ssl.verify=false` in your `versions.env` file:

```ini
# versions.env
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
The bundled `versions.env` at the repo root is found automatically.

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

1. **Resolve versions.** Reads `<repo>/versions.env` for the SBK and
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

A default `versions.env` ships with the project at the repository root and
carries both the **GitHub URL** and the **release tag** for each project:

```ini
# versions.env  (bundled at the project root)
sbk.url=https://github.com/kmgowda/SBK
sbk.version=10.0
sbk.folder=./.sbk
sbk.jdk.version=25
sbk.jdk.folder=./.jdk
ssl.verify=true

sbk-charts.url=https://github.com/kmgowda/sbk-charts
sbk-charts.version=4.26.6.1
```

Recognised keys (case-insensitive; dots / underscores / dashes interchangeable):

| Key | Required | Notes |
| --- | --- | --- |
| `sbk.url`        | no (defaults to `https://github.com/kmgowda/SBK`)        | Full URL `https://github.com/<owner>/<repo>` or `<owner>/<repo>` shorthand. |
| `sbk.version`    | yes | Tag that exists on that repository's Releases page. |
| `sbk.folder`     | no (defaults to `./.sbk`) | Local folder for SBK installation. |
| `sbk.jdk.version`| no (defaults to `25`) | Required JDK major version. |
| `sbk.jdk.folder`| no (defaults to `./.jdk`) | Local folder for JDK installation. |
| `ssl.verify`     | no (defaults to `true`) | Enable SSL verification for downloads. |
| `sbk-charts.url` | no (defaults to `https://github.com/kmgowda/sbk-charts`) | Same format as `sbk.url`. |
| `sbk-charts.version` | yes | Tag on the sbk-charts repository. |

You don't need to pass `-p` / `--properties` — `sbk-analytics` automatically
uses the bundled file. Pass `-p <path>` only if you want to override it
(e.g. to benchmark a fork of SBK):

```ini
# my-fork.env
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
| `-p`, `--properties` | path to the versions `.env` file (optional; defaults to bundled `<project>/versions.env`) |
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
  packaging quirks of sbk-charts 3.26.2.1. `sbk-analytics` already supplies a
  stub `banner.txt` via a private cwd; the missing logo is harmless.
- **All SBK instances failed; exit code 2** — none of the configured SBK runs
  produced a non-empty CSV. `sbk-charts` is intentionally **not** invoked in
  this case. Check the per-instance YAMLs under `<work-dir>/yml/` and re-run.

## Project layout

```
sbk-analytics/
├── versions.env              # bundled SBK / sbk-charts release pins
├── pyproject.toml            # entry point: sbk-analytics → analytics.cli:main
├── requirements.txt
├── README.md
├── examples/
│   ├── config.yml                  # generic multi-class example
│   └── file-rocksdb-write.yml      # 120s file + rocksdb single-writer example
└── analytics/
    ├── cli.py                # argument parsing + orchestration
    ├── properties.py         # versions.env parser
    ├── config.py             # input YAML parser (sbk, classes, sbk-charts)
    ├── releases.py           # GitHub release download + cached install
    ├── yaml_gen.py           # per-instance sbkArgs/sbkGemArgs YAML generator
    ├── runner.py             # serial / parallel SBK execution + watchdog
    ├── charts.py             # single sbk-charts invocation
    └── system_info.py        # appends `system` sheet to the final xlsx
```
