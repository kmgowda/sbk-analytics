# RocksDB benchmarks

These workflows benchmark the embedded SBK `rocksdb` driver with two controlled
datasets using 4 KiB and 64 KiB values. Run them in order:

```bash
./sbk-analytics -c examples/benchmarks/rocksdb/write.yml
./sbk-analytics -c examples/benchmarks/rocksdb/read.yml
```

`write.yml` clears `/tmp/sbk-analytics/benchmarks/rocksdb`, including previous
RocksDB databases, before writing. `read.yml` preserves and reads those exact
database paths with matching sizes and record counts. RocksDB results depend on
the filesystem, device, OS cache, WAL, compaction, compression, and sync
settings; record those environmental choices when comparing reports.

## SBK distribution compatibility

The workflows themselves are valid for the SBK RocksDB interface. During
end-to-end verification, the current SBK 10.6 release and the tested 10.7
development distribution both completed all requested records but failed while
closing RocksDB. Their classpath contains two different native libraries under
the same `librocksdbjni-linux64.so` resource name; the older shaded copy is
loaded before `rocksdbjni-6.10.2.jar`, which then lacks the `closeDatabase`
symbol expected by the Java class.

Use an SBK distribution whose RocksDB JNI resource collision is fixed for
report-producing RocksDB runs. sbk-analytics intentionally does not modify or
rebuild a supplied SBK distribution. The file-system examples are unaffected.
