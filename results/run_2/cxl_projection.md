# DEX CXL Projection: Measured NUMA Latencies → Estimated Throughput

Generated: 2026-05-03 14:48
Source data: `results/run_2`

## 1. Measured Environment

- **NPS mode**: NPS4
- **NUMA nodes**: 4
  - Node 0: 31753 MB
  - Node 1: 32251 MB
  - Node 2: 32251 MB
  - Node 3: 32235 MB

**NUMA distance matrix** (from `numactl --hardware`):

| | Node 0 | Node 1 | Node 2 | Node 3 |
|---|---:|---:|---:|---:|
| **Node 0** | 10 | 12 | 12 | 12 |
| **Node 1** | 12 | 10 | 12 | 12 |
| **Node 2** | 12 | 12 | 10 | 12 |
| **Node 3** | 12 | 12 | 12 | 10 |

**Pairwise idle latency** (MLC pointer chase, Phase 4):

| | → Node 0 | → Node 1 | → Node 2 | → Node 3 |
|---|---:|---:|---:|---:|
| **CPU on 0** | 110.1 ns | 121.0 ns | 131.5 ns | 135.5 ns |
| **CPU on 1** | 110.1 ns | 121.1 ns | 131.3 ns | 135.5 ns |
| **CPU on 2** | 110.2 ns | 121.1 ns | 131.4 ns | 135.5 ns |
| **CPU on 3** | 111.8 ns | 120.8 ns | 131.5 ns | 135.6 ns |

**Derived values** (min-local, max-remote — see note):

- Local DRAM latency (min of src==dst): **110.1 ns**
- Remote NUMA latency (max of src!=dst): **135.5 ns**
- Cross-domain penalty (Δ): **25.4 ns**
- Remote/local ratio: **1.23×**

> **Why min/max instead of averages?** Under NPS4, the NUMA domains are not
> symmetric — each CCD has a different distance to its memory controller,
> producing a gradient of latencies (e.g., 110→135 ns) rather than a clean
> local/remote split. Averaging collapses this gradient and can yield a near-
> zero cross-domain penalty. Min-local captures the best-case "local DRAM"
> baseline that CXL is compared against, and max-remote captures the worst-case
> intra-socket NUMA penalty — the natural floor for CXL emulation.

## 2. CXL Latency Mapping

CXL latency numbers are drawn from published measurements on real hardware.
Some sources report **ratios** relative to local DDR (these scale with your
hardware), while others report **absolute** latencies for specific products
(these are fixed device properties). The "Basis" column shows which.

| Label | Basis | Projected latency | Models | Source |
|-------|-------|------------------:|--------|--------|
| `native-numa` | measured | **136 ns** (native) | Measured remote NUMA (lower bound, no injection) | — |
| `cxl-asic` | 2.18× × 110 ns | **240 ns** (inject +105 ns) | ASIC CXL expander, same-socket (ratio from CXL-DMSim) | [A] CXL-DMSim |
| `cxl-fpga` | 2.88× × 110 ns | **317 ns** (inject +182 ns) | FPGA CXL prototype, same-socket (ratio from CXL-DMSim) | [A] CXL-DMSim |
| `cxl-switch` | absolute | **400 ns** (inject +264 ns) | CXL with switch, upper range of SupMario 4-device study | [B] SupMario |
| `cxl-pooled` | absolute | **596 ns** (inject +460 ns) | Samsung CMM-B pooled 8-device box (product spec) | [C] Samsung CMM-B |

For comparison, RDMA READ latency (ConnectX-5, InfiniBand): **~2000 ns**
(18× local DRAM)

## 3. Throughput Projection

### Method

From the DEX paper (Table 2), we know RDMA operations per index operation for each
system. We decompose per-thread time into:

```
per_op_time = rdma_time + compute_time
rdma_time   = (reads + writes) × 2000ns + atomics × 2000ns
compute_time = per_op_time − rdma_time  (held constant under CXL)
```

Under CXL, we replace the RDMA latency with the measured/projected CXL latency.
Two-sided RPC (offloading) is removed because real CXL expanders are passive.

### Read Only (skewed, 144 threads)

**Latency decomposition (RDMA baseline)**:

| | DEX | Sherman | SMART | P-Sherman | P-SMART |
|---|---:|---:|---:|---:|---:|
| RDMA ops / op | 0.33 | 3.02 | 1.44 | 1.00 | 1.15 |
| Paper throughput (Mops/s) | 60.0 | 17.0 | 6.3 | 24.0 | 7.1 |
| RDMA time (ns/op) | 661 | 6040 | 2880 | 2000 | 2300 |
| Compute time (ns/op) | 1739 | 2431 | 19977 | 4000 | 17982 |
| RDMA fraction | 28% | 71% | 13% | 33% | 11% |

**Projected throughput under CXL**:

| CXL scenario | Total latency | DEX | Sherman | SMART | P-Sherman | P-SMART |
|---|---:|---:|---:|---:|---:|---:|
| `native-numa` | 136 ns | 80.7 (1.35×) | 50.7 (2.98×) | 7.1 (1.13×) | 34.8 (1.45×) | 7.9 (1.12×) |
| `cxl-asic` | 240 ns | 79.2 (1.32×) | 45.6 (2.68×) | 7.1 (1.12×) | 34.0 (1.42×) | 7.9 (1.11×) |
| `cxl-fpga` | 317 ns | 78.1 (1.30×) | 42.5 (2.50×) | 7.0 (1.12×) | 33.4 (1.39×) | 7.8 (1.11×) |
| `cxl-switch` | 400 ns | 77.0 (1.28×) | 39.6 (2.33×) | 7.0 (1.11×) | 32.7 (1.36×) | 7.8 (1.10×) |
| `cxl-pooled` | 596 ns | 74.4 (1.24×) | 34.0 (2.00×) | 6.9 (1.10×) | 31.3 (1.31×) | 7.7 (1.09×) |
| RDMA baseline | 2000 ns | 60.0 (1.00×) | 17.0 (1.00×) | 6.3 (1.00×) | 24.0 (1.00×) | 7.1 (1.00×) |

### Write Intensive (skewed, 144 threads)

**Latency decomposition (RDMA baseline)**:

| | DEX | Sherman | SMART | P-Sherman | P-SMART |
|---|---:|---:|---:|---:|---:|
| RDMA ops / op | 0.52 | 4.29 | 1.67 | 1.52 | 1.29 |
| Paper throughput (Mops/s) | 45.0 | 5.0 | 6.5 | 6.5 | 6.5 |
| RDMA time (ns/op) | 1040 | 8580 | 3340 | 3040 | 2580 |
| Compute time (ns/op) | 2160 | 20220 | 18814 | 19114 | 19574 |
| RDMA fraction | 33% | 30% | 15% | 14% | 12% |

**Projected throughput under CXL**:

| CXL scenario | Total latency | DEX | Sherman | SMART | P-Sherman | P-SMART |
|---|---:|---:|---:|---:|---:|---:|
| `native-numa` | 136 ns | 64.6 (1.43×) | 6.9 (1.38×) | 7.6 (1.16×) | 7.5 (1.15×) | 7.3 (1.12×) |
| `cxl-asic` | 240 ns | 63.0 (1.40×) | 6.8 (1.36×) | 7.5 (1.15×) | 7.4 (1.14×) | 7.2 (1.11×) |
| `cxl-fpga` | 317 ns | 61.9 (1.38×) | 6.7 (1.33×) | 7.4 (1.15×) | 7.3 (1.13×) | 7.2 (1.11×) |
| `cxl-switch` | 400 ns | 60.8 (1.35×) | 6.6 (1.31×) | 7.4 (1.14×) | 7.3 (1.12×) | 7.2 (1.10×) |
| `cxl-pooled` | 596 ns | 58.3 (1.30×) | 6.3 (1.26×) | 7.3 (1.12×) | 7.2 (1.11×) | 7.1 (1.09×) |
| RDMA baseline | 2000 ns | 45.0 (1.00×) | 5.0 (1.00×) | 6.5 (1.00×) | 6.5 (1.00×) | 6.5 (1.00×) |

## 4. Competitive Gap Analysis

The key question: does DEX's advantage over Sherman shrink under CXL?

| Scenario | DEX (Mops/s) | Sherman (Mops/s) | DEX/Sherman ratio |
|----------|-------------:|-----------------:|------------------:|
| RDMA (paper) | 60.0 | 17.0 | 3.53× |
| CXL `native-numa` | 80.7 | 50.7 | 1.59× |
| CXL `cxl-asic` | 79.2 | 45.6 | 1.74× |
| CXL `cxl-fpga` | 78.1 | 42.5 | 1.84× |

Under RDMA, DEX's optimizations are worth a ~3.5× advantage. Under CXL, the gap
narrows because Sherman's bottleneck (expensive RDMA reads) largely disappears.
DEX's optimizations still help, but the marginal value drops from "essential" to
"incremental."

## 5. Implications for DEX's Three Pillars

Using the `cxl-asic` scenario (240 ns, 2.18× local DRAM per CXL-DMSim) as the reference point:

### Logical Partitioning

- RDMA CAS cost: ~2000 ns → CXL CPU CAS cost: ~240 ns
- Speedup on atomics: **8.3×** cheaper
- Partitioning exists to avoid cross-server RDMA CAS. At this cost reduction,
  shared-node locking is cheap enough that the partitioning overhead (load
  imbalance, repartitioning cost) may not be justified.

### Cooling Map Cache

- DRAM-to-RDMA gap: ~18× (drove 18× replacement frequency argument)
- DRAM-to-CXL gap: ~2.2×
- At a 2–3× gap, cache miss penalty is low enough that a simple LRU or CLOCK
  may perform within 10% of the cooling map's bucket-level FIFO.

### Offloading

- Real CXL memory expanders are passive (no CPU) → offloading is impossible.
- Even with hypothetical memory-side compute, the one-sided path (direct load/store)
  at 240 ns is fast enough that the RPC overhead of offloading rarely wins.
- The DEX paper's offloading cost model: `l_p < (L+1) × (l_o + l_s) × c`.
  Substituting l_o = 240 ns (was 2000 ns) makes the inequality almost never satisfied.

## 6. Caveats

This is an upper-bound analytical model. Key limitations:

1. **Compute time is assumed constant.** In reality, removing RDMA may shift the
   bottleneck to CPU cache contention, memory bandwidth, or lock contention.
2. **NUMA ≠ CXL.** Intra-socket NUMA shares L3 cache across domains; real CXL does not.
   This makes our emulation slightly faster than real CXL for L3-friendly workloads.
3. **Bandwidth is not modeled.** CXL devices have constrained bandwidth (20–80% of local DDR).
   Scan-intensive workloads would be affected more than this model predicts.
4. **Contention dynamics change.** Sherman's CAS retry storms under RDMA resolve
   differently under CXL — contention shifts from network to CPU cache lines,
   which may be better or worse depending on core count and LLC topology.
5. **Cache hit rate may change.** Lower miss penalty under CXL could make aggressive
   caching less important, changing DEX's behavior in ways this model doesn't capture.

## 7. Next Steps

1. **Run the POC** (`cxl_poc.cpp`) to get real traversal-level measurements and validate this model.
2. **Sweep delay injection** — use `--delay 105` for ASIC CXL and `--delay 182` for FPGA CXL (computed from multipliers above).
3. **Build the shim layer** (Approach A from the design doc) to get real DEX-on-CXL numbers.
4. **Ablation study**: measure DEX with partitioning/caching/offloading individually disabled
   under CXL to quantify each pillar's residual value.

## 8. CXL Latency Sources

The CXL latency sweep points in this report are derived from the following published measurements:

| Key | Paper | Measurement | Value | How used |
|-----|-------|-------------|-------|----------|
| [A] | CXL-DMSim (2024), arXiv:2411.02282 | ASIC CXL expander vs local DDR | 2.18× local DDR | **Ratio** — scaled by your measured local DRAM |
| [A] | CXL-DMSim (2024), arXiv:2411.02282 | FPGA CXL expander vs local DDR | 2.88× local DDR | **Ratio** — scaled by your measured local DRAM |
| [B] | SupMario / Liu et al. (2024), arXiv:2409.14317 | 4 real CXL devices, locally-attached | 200–400 ns | **Absolute** — 400ns used for cxl-switch |
| [B] | SupMario / Liu et al. (2024), arXiv:2409.14317 | CXL with switch | ~600 ns | **Absolute** |
| [C] | Samsung CMM-B spec (Memcon 2024) | 8-device pooled CXL box | 596 ns | **Absolute** — device property, not system-dependent |
| [D] | M²NDP (2024), arXiv:2404.19381 | Optimized ASIC load-to-use | 150–175 ns | Reference only (confirms [A] range) |
| [E] | CXLAimPod (2025), arXiv:2508.15980 | CXL vs DDR5 | 130–200 ns vs 75–85 ns | Reference only (confirms [A] range) |

**DEX paper**: Lu et al., "DEX: Scalable Range Indexing on Disaggregated Memory," PVLDB 17(10), 2024.
RDMA latency (~2000 ns) from Section 2.2; Table 2 RDMA statistics under 144 threads.
