# MinIO, Dell ECS, and ObjectScale benchmarks

These persistent workflows exercise SBK's `minio` class against an
S3-compatible service. The supplied ECS/OBS examples use the four HTTP data
endpoints in the authorized lab and the dedicated `sbk-ns` namespace. They do
not contain management, SSH, or S3 credentials.

## Credential and endpoint contract

Export credentials only in the launching shell. SBK 10.7 reads these variables
when `key`, `secret`, or `gempass` are absent from the generated SBK YAML:

```bash
export SBK_S3_ACCESS_KEY='<dedicated ECS Object User>'
export SBK_S3_SECRET_KEY='<Object User secret>'
export SBK_GEM_SSH_PASSWD='<load-generator SSH password>' # GEM only
```

The ECS management account is not an S3 identity. Use it only to provision a
dedicated namespace/Object User. Do not add any secret to a committed workflow,
terminal transcript, generated report, or issue.

ECS normally exposes S3 over HTTP `9020` or HTTPS `9021`. A root request should
identify the data plane with S3 XML—often HTTP 403 `AccessDenied`. HTML,
management JSON, or HTTP 405 usually means the wrong port.

## ECS/OBS preparation

Use the ECS management UI only for control-plane setup:

1. Sign in to the management endpoint on port `4443` with an authorized
   administrator account.
2. Create or select a benchmark-only namespace. The lab workflows use
   `sbk-ns`.
3. Create a benchmark-only S3 Object User in that namespace and generate its
   secret key. An ECS management user is not an S3 Object User.
4. Grant only the bucket permissions required by the test. Export the Object
   User name and secret through `SBK_S3_ACCESS_KEY` and
   `SBK_S3_SECRET_KEY` in the controller shell.
5. Use a dedicated bucket. The examples use `sbk-analytics-ecs-obs`; SBK can
   create it on the first PUT when the Object User has permission.

For another MinIO, ECS, or ObjectScale installation, copy a workflow outside
the repository and replace the `url` endpoint pool, `bucket`, namespace header,
and GEM node inventory. A native MinIO deployment normally does not need the
ECS-specific `x-emc-namespace` header.

## SBK 10.7 workflow contract

SBK 10.7 uses only `url` for S3 target selection. It accepts either one S3 URL
or a comma-separated pool distributed round-robin across workers. The former
standalone `endpoint` and `endpoints` keys are not supported; sbk-analytics
rejects both spellings with guidance to replace them with `url`.

The committed workflows enable `endpoint-preflight: all` and
`endpoint-metrics: true`. Preflight checks every configured URL before timing;
endpoint metrics retain per-URL logical operations, bytes, retries, and
terminal failures. The workflows begin with one-attempt retry policy so
backend saturation is not hidden by client retries.

SBK 10.7 also supports these persistent workload controls:

| Goal | YAML options |
| --- | --- |
| Reproducible object sizes | `object-size-distribution: fixed|uniform:min:max|sweep:min:max|weighted:...` and `data-seed` |
| Reproducible key layout | `key-distribution: sequential|hashed|random`, optional `partition-by-prefix` |
| Range GET shape | `range-offset-distribution`, `range-window-length`, `range-alignment` |
| LIST shape | `list-max-keys`, `list-max-entries`, `list-api-version`, delimiter/start-after/owner/metadata controls |
| Retry policy | `retry-max-attempts`, `retry-strategy`, `retry-backoff-ms`, `retry-max-backoff-ms`, `retry-jitter` |
| Untimed connection/data warm-up | `warmup-requests` and `warmup-operation: connection|put|get|put-get` |
| Audit record | `run-manifest` writes a credential-free effective-workload JSON file |

`mixed-read-source` accepts only `catalog`; the unsafe former `published` mode
is intentionally rejected. `auth-version` accepts only SigV4 value `4`.
SBK remains authoritative for numeric bounds, catalog capacity, multipart,
async-memory, operation-mix, permissions, and backend-state validation.

```mermaid
flowchart LR
    A["sbk-analytics controller"] -->|"sbk-yal: qualification / throughput"| E["ECS S3 data endpoints<br/>:9020 or :9021"]
    A -->|"sbk-gem-yal: SSH provisioning"| G["load-generator nodes"]
    G -->|"distributed S3 operations"| E
    A --> C["CSV results"]
    C --> H["one sbk-charts workbook"]
    M["ECS management UI :4443"] -. "provision namespace,<br/>Object User, bucket policy" .-> E
```

## Workflow catalog and order

Every file is one persistent sbk-analytics workflow containing multiple named
SBK instances and exactly one final sbk-charts invocation. Options common to
all instances remain under `sbk:`; the operation-specific options are nested
under each named `minio:` instance.

| Workflow | Purpose | Instances | Prerequisite |
| --- | --- | ---: | --- |
| `ecs-obs-qualification.yml` | Small PUT, GET, Range GET, LIST, and multipart correctness gate | 5 | Existing dedicated bucket |
| `ecs-obs-object-shapes.yml` | Uniform, short byte-sweep, weighted sizes, sequential/hashed/random keys, and filesystem-style keys | 4 | Existing dedicated bucket |
| `ecs-obs-async-concurrency.yml` | Fixed-count synchronous versus bounded-asynchronous PUT and GET | 4 | Existing dedicated bucket; PUT instances seed both read prefixes |
| `ecs-obs-api-operations.yml` | PUT, HEAD/stat, tag get/set/delete, overwrite, server-side copy, and LIST | 8 | Existing dedicated bucket; preserve the declared serial order |
| `ecs-obs-range-list-metadata.yml` | Sequential/random aligned Range GET, HEAD/stat, LIST v1 delimiter, and LIST v2 owner/user metadata | 6 | Existing dedicated bucket; first instance seeds the prefix |
| `ecs-obs-data-profiles.yml` | Compressibility/dedup payload shapes and sequential/concurrent multipart parts | 5 | Existing dedicated bucket |
| `ecs-obs-mixed-operations.yml` | Deterministic weighted read/write mixes and simultaneous readers/writers | 4 | Existing dedicated bucket; first instance seeds the catalog |
| `ecs-obs-throughput.yml` | Short duration-based four-endpoint PUT/GET load | 2 | Qualification passed; understand duration-boundary behavior |
| `ecs-obs-gem.yml` | Distributed SBK-GEM PUT/GET | 2 | Ordinary workflow passes from every client; controller SSH works |

Run qualification first, then select the focused workflow that represents the
application behavior being studied. Do not merge all examples into one large
capacity run: changing object size, concurrency, operation type, and payload
shape simultaneously makes attribution difficult.

```bash
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-qualification.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-object-shapes.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-async-concurrency.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-api-operations.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-range-list-metadata.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-data-profiles.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-mixed-operations.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-throughput.yml
./sbk-analytics -c examples/benchmarks/minio/ecs-obs-gem.yml
```

`cleanup_before_run` only clears the local analytics work directory. It does
not remove ECS objects or buckets. Each benchmark uses an explicit prefix so
test data remains attributable. Do not add `recreate`, delete, or bucket-delete
to a persistent workflow unless the exact disposable target is independently
approved.

The focused examples intentionally use fixed record counts. This makes exact
completion auditable and avoids classifying cancellation of an in-flight S3
request at a duration boundary as a backend failure. Use the duration-based
throughput workflow only after fixed-count correctness passes.

### What the focused options mean

- Object-size `sweep` advances one byte per logical operation. The short
  example therefore uses `sweep:4096:4105`; use a much larger record count if
  benchmarking a wide sweep. Weighted values are literal deterministic cycle
  weights, so the example uses a short `2:1:1` cycle that is visible in forty
  records.
- Endpoint assignment is worker based. Four workers exercise all four URLs;
  two multipart writers use two URLs even when each object has four concurrent
  parts. Per-part concurrency does not change endpoint assignment.
- Metadata operations (`stat`, tags, and LIST) correctly report zero payload
  MB. Compare records/sec and latency, not MB/sec.
- COPY reports logical source-object bytes, but the payload stays inside ECS.
  It measures server-side copy completion rather than controller network
  throughput.
- Async depth is bounded per worker and again at process scope. Increase
  `async-depth`, `async-max-inflight`, and `async-max-memory-mb` together only
  after confirming client memory and ECS tail latency remain acceptable.
- Mixed operation weights are deterministic cycles, not random percentages.
  `mixed-read-source: catalog` uses the bounded startup snapshot populated by
  the preceding seed instance.
- Payload compressibility and deduplication options describe generated bytes;
  they do not prove that ECS compressed or deduplicated them internally. Pair
  client results with ECS telemetry for storage-efficiency conclusions.

## Reading results

Treat a run as valid only when all of these hold:

- sbk-analytics, SBK/SBK-GEM, and sbk-charts exit successfully;
- every requested fixed record is present in the Total result;
- output contains no S3/I/O failures or unexpected retries;
- read-size verification succeeds and no invalid/discarded latency appears;
- the load generator is not the unintended bottleneck;
- the raw CSV, generated workbook, exact workflow, dependency provenance, and
  cluster/load-host context are retained together.

PUT/GET report object operations and payload throughput. Range GET reports the
selected range bytes. LIST's byte rate is the logical size of listed objects,
not response-wire bandwidth; compare LIST operations/sec and latency instead.
For credible performance claims, warm up first and run at least three measured
repetitions long enough to reach steady state. The short shipped workflows are
qualification examples, not product performance specifications.

## Expanded SBK 10.7 option-suite validation (2026-09-10)

All 31 instances in the six focused workflows were executed from one
controller against ECS `10.236.66.181` through `.184`. Every instance returned
zero, produced its exact fixed record count, reported zero retries and terminal
S3 failures, and contributed a CSV to its workflow's single sbk-charts
workbook. The test used SBK 10.7, JDK 25.0.2, sbk-charts 4.26.7.1, and a
temporary Object User/bucket in `sbk-ns`; all 1,008 test objects, the
credential, bucket, and Object User were removed after validation.

The values below are one-run integration evidence, not ECS capacity claims.
Metadata and LIST operations transfer no object payload, so their MB/s is
shown as `n/a`.

| Workflow / instance | Records | MB/s | Records/s | Average | p99 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Object shapes / uniform sequential PUT | 40 | 0.21 | 6.5 | 584.8 ms | 883 ms |
| Object shapes / 4 KiB byte-sweep hashed PUT | 40 | 0.05 | 12.9 | 288.2 ms | 327 ms |
| Object shapes / weighted random PUT | 40 | 1.84 | 6.9 | 536.2 ms | 1993 ms |
| Object shapes / filesystem-layout PUT | 40 | 0.11 | 6.8 | 545.2 ms | 604 ms |
| Concurrency / synchronous PUT 64 KiB | 80 | 0.40 | 6.4 | 595.9 ms | 934 ms |
| Concurrency / bounded-async PUT 64 KiB | 80 | 1.38 | 22.1 | 660.3 ms | 1223 ms |
| Concurrency / synchronous GET 64 KiB | 80 | 0.53 | 8.5 | 361.6 ms | 2916 ms |
| Concurrency / bounded-async GET 64 KiB | 80 | 1.84 | 29.4 | 482.7 ms | 1305 ms |
| API operations / tagged PUT seed | 40 | 0.37 | 5.9 | 642.9 ms | 948 ms |
| API operations / HEAD-stat | 40 | n/a | 13.3 | 282.4 ms | 304 ms |
| API operations / tag GET | 40 | n/a | 13.9 | 279.9 ms | 313 ms |
| API operations / overwrite | 40 | 0.40 | 6.5 | 577.3 ms | 931 ms |
| API operations / server-side copy | 40 | 0.82¹ | 13.1 | 293.0 ms | 321 ms |
| API operations / tag set | 40 | n/a | 13.3 | 289.1 ms | 365 ms |
| API operations / tag delete | 40 | n/a | 13.5 | 276.5 ms | 351 ms |
| API operations / copied-prefix LIST | 5 | n/a | 1.8 | 553.4 ms | 621 ms |
| Range/LIST / 1 MiB PUT seed | 80 | 3.89 | 3.9 | 729.1 ms | 5120 ms |
| Range/LIST / sequential aligned Range GET | 80 | 0.05 | 13.1 | 286.7 ms | 372 ms |
| Range/LIST / random aligned Range GET | 80 | 0.05 | 13.4 | 280.0 ms | 303 ms |
| Range/LIST / HEAD-stat | 80 | n/a | 13.8 | 284.8 ms | 304 ms |
| Range/LIST / recursive LIST v2 + metadata | 5 | n/a | 0.7 | 1336.4 ms | 1522 ms |
| Range/LIST / delimiter LIST v1 | 5 | n/a | 2.8 | 351.0 ms | 419 ms |
| Data profiles / incompressible anti-dedup PUT | 40 | 1.35 | 5.4 | 665.6 ms | 1568 ms |
| Data profiles / 50% compressible anti-dedup PUT | 40 | 1.42 | 5.7 | 681.8 ms | 1492 ms |
| Data profiles / compressible dedup-friendly PUT | 40 | 1.45 | 5.8 | 669.1 ms | 1499 ms |
| Data profiles / sequential multipart parts | 4 | 9.96 | 0.3 | 6106.5 ms | 7602 ms |
| Data profiles / four concurrent multipart parts | 4 | 11.64 | 0.4 | 4197.3 ms | 7053 ms |
| Mixed operations / tagged PUT seed | 80 | 0.41 | 6.5 | 566.1 ms | 878 ms |
| Mixed operations / GET-stat-tag read mix | 80 | 0.47¹ | 7.5 | 406.9 ms | 977 ms |
| Mixed operations / PUT-update-copy write mix | 80 | 0.32¹ | 5.1 | 753.8 ms | 1070 ms |
| Mixed operations / simultaneous readers/writers | 80 | 0.47¹ | 7.5 | 263.6 ms | 742 ms |

¹ Mixed and server-side operations report logical object bytes; do not treat
this as controller wire throughput.

These short comparisons demonstrate feature operation, not statistical
superiority. For example, bounded async improved records/sec in this run but
also changed latency distribution. Repeat each selected workload at least
three times under controlled cluster health and client conditions before
drawing performance conclusions.

## SBK 10.7 lab validation

The current workflows were validated on 2026-09-09 against ECS endpoints
`10.236.66.181` through `.184`, using SBK 10.7, Temurin JDK 25.0.2, and the
managed sbk-charts 4.26.7.1 package. The managed SBK archive was resolved from
GitHub tag `v10.7` and passed its published SHA-256 check. Credentials came
from a dedicated Object User in `sbk-ns`; no credential was written to the
repository, workflow, report, or test log.

The fixed-count qualification passed all five instances (PUT, GET, Range GET,
LIST, and multipart PUT), produced five CSV files, and generated
`ecs-obs-qualification.xlsx`. The four-endpoint workflow passed both 30-second
instances, reported operations on every configured endpoint with zero retries
and zero terminal failures, produced two CSV files, and generated
`ecs-obs-throughput.xlsx`.

| Workflow / operation | Load | Records | MB/s | Records/s | Average | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qualification PUT | 1 writer, 1 MiB, fixed | 20 | 1.46 | 1.5 | 686.4 ms | 2097 ms | 2097 ms |
| Qualification GET | 1 reader, 1 MiB, fixed | 20 | 2.75 | 2.8 | 363.3 ms | 1921 ms | 1921 ms |
| Qualification Range GET | 1 reader, 4 KiB range, fixed | 20 | 0.01 | 3.5 | 281.8 ms | 285 ms | 285 ms |
| Qualification LIST | 1 reader, fixed | 5 | n/a¹ | 3.1 | 317.0 ms | 382 ms | 382 ms |
| Qualification multipart PUT | 1 writer, 15 MiB object / 5 MiB parts | 2 | 5.07 | 0.3 | 2942.5 ms | 4003 ms | 4003 ms |
| Four-endpoint PUT | 4 writers, 1 MiB, 30 s | 193 | 5.96 | 6.0 | 661.0 ms | 2097 ms | 2322 ms |
| Four-endpoint GET | 4 readers, 1 MiB, 30 s | 326 | 10.85 | 10.9 | 366.7 ms | 679 ms | 2066 ms |

¹ LIST byte rate is not response-wire throughput; use operations/sec and
latency for comparisons.

These are single-run integration results, not capacity claims. The controller
could reach all four S3 endpoints, but TCP port 22 remained unreachable on all
eight supplied load generators (`10.236.65.98` through `.105`). The documented
GEM precondition therefore failed and the distributed workflow was not
started. Restore controller-to-client SSH routing, validate the ordinary
workload independently from every client, and then run `ecs-obs-gem.yml`.

## Historical SBK 10.6 lab record

On 2026-09-02 all four supplied endpoints (`10.236.66.181` through `.184`) on
port `9020` returned HTTP 403 with `application/xml`, confirming reachable S3
data-plane services. Management authentication on `.181:4443` succeeded and
reported the existing `sbk-ns` namespace.

The controller could not route to SSH port 22 on any supplied load generator
(`10.236.65.98` through `.105`), so distributed SBK-GEM performance results
must not be claimed from that attempt. After correcting analytics to select
the GEM `GemPrometheusLogger`, GEM reached its SSH connection phase, reported
`No route to host` for the nodes, exited non-zero, skipped charts because no CSV
was produced, and left no locally running SBK/GEM process.

| Scenario | Status | Evidence |
| --- | --- | --- |
| Four ECS S3 endpoints | Qualified preflight | HTTP 403, `application/xml`, 0.27–0.28 s connect, 0.54–0.56 s total |
| ECS management | Qualified preflight | Login HTTP 200; `sbk-ns` discovered |
| SBK 10.6 MinIO contract | Qualified end to end | Environment credentials, fixed/timed, multipart, Range GET, LIST, and multi-endpoint workflows completed; see the endpoint-metrics limitation below |
| Qualification workflow | Passed | 5/5 SBK instances passed; 5 CSV files and `ecs-obs-qualification.xlsx` created |
| Throughput workflow | Passed | PUT and GET passed; 2 CSV files and `ecs-obs-throughput.xlsx` created |
| SBK-GEM load nodes | Blocked | SSH returned `No route to host` for all eight supplied nodes |

### Example results

These results were produced on 2026-09-02 by the committed workflows from one
controller using SBK 10.6, Temurin JDK 25.0.2, and the managed sbk-charts
package. They demonstrate a working workflow and provide a regression
reference only; they are not an ECS capacity or product benchmark. Each
scenario was run once, without a controlled warm-up or a recorded ECS health
snapshot.

| Workflow / operation | Load | Records | MB/s | Records/s | Average | p95 | p99 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Qualification PUT | 1 writer, 1 MiB, fixed | 20 | 1.64 | 1.6 | 608.6 ms | 1845 ms | 1845 ms |
| Qualification GET | 1 reader, 1 MiB, fixed | 20 | 2.05 | 2.1 | 486.8 ms | 2296 ms | 2296 ms |
| Qualification Range GET | 1 reader, 4 KiB range, fixed | 20 | 0.02 | 3.9 | 257.2 ms | 261 ms | 261 ms |
| Qualification LIST | 1 reader, fixed | 5 | n/a¹ | 3.2 | 314.6 ms | 380 ms | 380 ms |
| Qualification multipart PUT | 1 writer, 15 MiB object / 5 MiB parts | 2 | 2.54 | 0.2 | 5895.0 ms | 8427 ms | 8427 ms |
| Four-endpoint PUT | 4 writers, 1 MiB, 30 s | 187 | 6.23 | 6.2 | 633.7 ms | 676 ms | 2696 ms |
| Four-endpoint GET | 4 readers, 1 MiB, 30 s | 300 | 10.00 | 10.0 | 395.3 ms | 580 ms | 2546 ms |

¹ SBK reports a logical LIST MB rate, but it is not response-wire throughput;
use operations/sec and latency for LIST comparisons.

The charts installer identified the configured GitHub source tag as
`4.26.7.1`, while that package's runtime banner printed `4.26.6.3`. The
workbooks were created successfully, but retain both values in any audit trail
until the upstream package banner is corrected.

For publishable measurements, record operation, size, concurrency,
duration/count, at least three repetitions, throughput, average/p50/p95/p99
latency, endpoint retries/failures, exact SBK/JDK/charts provenance, client
topology, network path, and ECS health state.

### Endpoint-metrics history

The SBK 10.6 release help advertised `endpoint-metrics`, but its packaged
MinIO argument parser rejected it. SBK 10.7 implements and validates the
option, and the current workflows require it. Do not infer per-endpoint
counters from aggregate CSV data when reviewing an older result.

## Troubleshooting checklist

- `AccessDenied`: confirm the S3 Object User, secret, namespace membership,
  bucket policy, and `x-emc-namespace` value; do not use management credentials.
- `NoSuchBucket`: run the qualification PUT first or create the dedicated
  bucket through approved ECS administration.
- `No route to host` during GEM: fix controller-to-load-generator routing and
  port 22 access before changing SBK parameters.
- GEM logger class failure: use this version of sbk-analytics; current SBK GEM
  workflows require `GemPrometheusLogger`, which analytics selects automatically.
- Empty/missing CSV: treat the instance as failed and inspect its exit code and
  generated YAML. sbk-charts is intentionally skipped if every workload fails.
- Unexpected capacity numbers: confirm steady state, repetitions, client CPU
  and network headroom, endpoint health, and that aggregate results are not
  being presented as per-endpoint measurements.
