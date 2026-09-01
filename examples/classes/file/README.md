# File-system benchmarks

These workflows benchmark the SBK `file` driver with two controlled 256 MiB
datasets:

- 65,536 records × 4 KiB
- 4,096 records × 64 KiB

Run them in order:

```bash
./sbk-analytics -c examples/classes/file/write.yml
./sbk-analytics -c examples/classes/file/read.yml
```

`write.yml` clears `/tmp/sbk-analytics/classes/file` before preparing the two
files. `read.yml` deliberately preserves that directory and reads the same
paths with identical sizes and record counts. Re-running the write workflow
starts a new write/read cycle. Reports are written beside the generated YAML,
CSV, and log artifacts in the class workdir.
