# Storage class benchmark examples

This catalog keeps persistent sbk-analytics workflows grouped by SBK storage
class. Each class directory documents its preparation and measurement order.

| Class | Workflows | Data location |
| --- | --- | --- |
| [file](file/README.md) | Fixed-size filesystem write and read | `/tmp/sbk-analytics/benchmarks/file` |
| [rocksdb](rocksdb/README.md) | Fixed-size RocksDB write and read | `/tmp/sbk-analytics/benchmarks/rocksdb` |
| [minio / ECS / ObjectScale](minio/README.md) | S3 qualification, multi-endpoint throughput, and distributed GEM | `/tmp/sbk-analytics/benchmarks/minio` |

Run each class's write workflow before its read workflow. The write workflow
uses `cleanup_before_run: true` to start with fresh class data. The corresponding
read workflow uses `false` so it can measure the data produced by the write.

Each workflow contains two named SBK instances—4 KiB and 64 KiB records—and
generates one combined sbk-charts report. They use serial mode by default to
avoid cross-instance storage contention; change `mode` only when concurrent
load is the intended experiment.

Network storage classes have their own ordering and safety contract. Follow the
class README rather than assuming the local file/RocksDB write-then-read rules.
