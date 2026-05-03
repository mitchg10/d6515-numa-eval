#!/usr/bin/env python3
"""
cxl_projection.py — Parse NUMA measurement results and generate a CXL
throughput projection document for DEX.

Reads output files from measure_numa_latency.sh, extracts local and remote
NUMA latencies, then applies the Table 2 decomposition model from the DEX
paper (Lu et al., VLDB 2024) to project DEX/Sherman/SMART throughput under
CXL emulation.

Usage:
    python3 cxl_projection.py <results_dir> [--output report.md]

    <results_dir>  Path to timestamped output of measure_numa_latency.sh
                   (e.g., ~/numa-results/20250503_143022)

Expects these files in <results_dir>:
    pairwise_latency.txt       (Phase 4 output — required)
    idle_latency_matrix.txt    (Phase 2 — used for cross-check)
    nps_mode.txt               (Phase 1 — NPS mode label)
    topology.txt               (Phase 0 — numactl --hardware output)
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════
# DEX paper constants (Table 2, Section 8, Figures 6–7)
# ═══════════════════════════════════════════════════════════════════

RDMA_LATENCY_NS = 2000  # paper Section 2.2: "RDMA exhibits higher latency (~2000ns)"

# Table 2: RDMA statistics per index operation (skewed, 144 threads)
# Format: { system: { workload: (reads, writes, atomics, twosided, paper_mops, paper_threads) } }
TABLE2 = {
    "DEX": {
        "read-only":       (0.33, 0.00, 0.00, 0.0002, 60.0, 144),
        "write-intensive": (0.33, 0.19, 0.00, 0.0001, 45.0, 144),
    },
    "Sherman": {
        "read-only":       (3.02, 0.00, 0.00, 0.0,    17.0, 144),
        "write-intensive": (2.71, 0.99, 0.59, 0.0,     5.0, 144),
    },
    "SMART": {
        "read-only":       (1.44, 0.00, 0.00, 0.0,     6.3, 144),
        "write-intensive": (1.45, 0.11, 0.11, 0.0,     6.5, 144),
    },
    "P-Sherman": {
        "read-only":       (1.00, 0.00, 0.00, 0.0,    24.0, 144),
        "write-intensive": (1.02, 0.50, 0.00, 0.0,     6.5, 144),
    },
    "P-SMART": {
        "read-only":       (1.15, 0.00, 0.00, 0.0,     7.1, 144),
        "write-intensive": (1.16, 0.13, 0.00, 0.0,     6.5, 144),  # estimated
    },
}

# CXL latency sweep points — two kinds of source data:
#   "mult" = ratio of CXL-to-local-DDR measured on the SAME system (scales with your hardware)
#   "abs"  = absolute latency from a product spec or device measurement (fixed)
#
# Sources:
#   [A] CXL-DMSim (2024), arXiv:2411.02282
#       Measured on same system: ASIC CXL = 2.18× local DDR, FPGA CXL = 2.88× local DDR
#   [B] SupMario / Liu et al. (Virginia Tech/Microsoft, 2024), arXiv:2409.14317
#       4 real CXL devices: locally-attached range ~200–400ns (absolute measurements)
#       CXL with switch: ~600ns (absolute)
#   [C] Samsung CMM-B product spec (Memcon 2024)
#       596ns absolute for pooled 8-device box
#   [D] M²NDP (2024), arXiv:2404.19381: load-to-use 150–175ns for optimized ASIC
#   [E] CXLAimPod (2025), arXiv:2508.15980: 130–200ns vs DDR 75–85ns
#
# Format: (label, kind, value, description, source_key)
#   kind="mult": value is a multiplier of measured local DRAM
#   kind="abs":  value is absolute latency in ns (device property, not system-dependent)
#   kind=None:   use measured remote NUMA directly
CXL_SWEEP = [
    ("native-numa",  None,   0,     "Measured remote NUMA (lower bound, no injection)",        "—"),
    ("cxl-asic",     "mult", 2.18,  "ASIC CXL expander, same-socket (ratio from CXL-DMSim)",  "[A] CXL-DMSim"),
    ("cxl-fpga",     "mult", 2.88,  "FPGA CXL prototype, same-socket (ratio from CXL-DMSim)", "[A] CXL-DMSim"),
    ("cxl-switch",   "abs",  400,   "CXL with switch, upper range of SupMario 4-device study", "[B] SupMario"),
    ("cxl-pooled",   "abs",  596,   "Samsung CMM-B pooled 8-device box (product spec)",        "[C] Samsung CMM-B"),
]


def compute_cxl_latencies(local_ns, remote_ns):
    """Compute concrete CXL latencies from multipliers or absolute values."""
    result = []
    for label, kind, value, desc, source in CXL_SWEEP:
        if kind is None:
            lat = remote_ns
            basis = "measured"
        elif kind == "mult":
            lat = local_ns * value
            basis = f"{value}× × {local_ns:.0f} ns"
        elif kind == "abs":
            lat = value
            basis = "absolute"
        else:
            raise ValueError(f"Unknown kind: {kind}")
        result.append((label, lat, basis, desc, source))
    return result


# ═══════════════════════════════════════════════════════════════════
# Parsing
# ═══════════════════════════════════════════════════════════════════

def parse_pairwise(filepath):
    """Parse pairwise_latency.txt → dict of (src, dst) → latency_ns."""
    pairs = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 3:
                try:
                    src, dst = int(parts[0]), int(parts[1])
                    lat = float(parts[2])
                    pairs[(src, dst)] = lat
                except (ValueError, IndexError):
                    continue
    return pairs


def parse_nps_mode(filepath):
    """Extract NPS mode string from nps_mode.txt."""
    if not os.path.exists(filepath):
        return "unknown"
    with open(filepath) as f:
        for line in f:
            m = re.search(r"NPS_MODE=(\S+)", line)
            if m:
                return m.group(1)
    return "unknown"


def parse_topology(filepath):
    """Extract node sizes and distances from numactl --hardware output."""
    info = {"nodes": {}, "distances": []}
    if not os.path.exists(filepath):
        return info
    with open(filepath) as f:
        content = f.read()
    # Node sizes
    for m in re.finditer(r"node\s+(\d+)\s+size:\s+(\d+)\s+MB", content):
        info["nodes"][int(m.group(1))] = int(m.group(2))
    # Distance table
    dist_section = False
    for line in content.split("\n"):
        if "node distances" in line.lower():
            dist_section = True
            continue
        if dist_section and re.match(r"\s*\d+:", line.strip()):
            parts = line.strip().split()
            info["distances"].append([int(x) for x in parts[1:]])
    return info


def get_local_remote_latency(pairs):
    """From pairwise measurements, extract representative local and remote latencies.

    We use the MINIMUM local (src==dst) latency and MAXIMUM cross-domain
    (src!=dst) latency rather than averaging. Why:

    Under NPS4 on EPYC 7452, the 4 NUMA domains are not symmetric — each CCD
    has a different physical distance to its assigned memory controller. This
    produces a gradient of "local" latencies (e.g., 110, 121, 131, 135 ns) with
    no clean local-vs-remote separation. Averaging all local and all remote
    values collapses this gradient: in the NPS4 run, averaging gave a "cross-
    domain penalty" of 0.1 ns, which is meaningless.

    Using min-local and max-remote captures the true extremes of the memory
    hierarchy:
      - min-local = best case: thread accessing its own closest memory controller
        → this is the "local DRAM" baseline that CXL latency is compared against
      - max-remote = worst case: thread accessing the farthest NUMA domain
        → this is the upper bound on intra-socket NUMA penalty and the natural
          lower bound for CXL emulation (real CXL would be at least this slow)

    The difference (max_remote - min_local) gives the maximum observable NUMA
    penalty on this hardware, which is the quantity we actually care about for
    CXL emulation: how much slower is "far" memory than "near" memory?
    """
    local_lats = []
    remote_lats = []
    for (src, dst), lat in pairs.items():
        if lat == "N/A":
            continue
        if src == dst:
            local_lats.append(lat)
        else:
            remote_lats.append(lat)

    local_best = min(local_lats) if local_lats else None
    remote_worst = max(remote_lats) if remote_lats else None
    return local_best, remote_worst


# ═══════════════════════════════════════════════════════════════════
# Projection model
# ═══════════════════════════════════════════════════════════════════

def project_system(name, workload, cxl_read_ns, cxl_cas_ns):
    """
    Project throughput for one system under one workload at a given CXL latency.

    Model:
        per_thread_time = rdma_time + compute_time           (RDMA)
        per_thread_time_cxl = cxl_time + compute_time        (CXL)

    Where:
        rdma_time = (reads + writes) * RDMA_READ_ns + atomics * RDMA_CAS_ns + twosided * RPC_ns
        cxl_time  = (reads + writes) * cxl_read_ns + atomics * cxl_cas_ns
        (twosided → 0 under CXL because offloading is removed for passive expanders)
    """
    reads, writes, atomics, twosided, paper_mops, threads = TABLE2[name][workload]

    # RDMA decomposition
    # Treat reads and writes as same latency (RDMA READ/WRITE ≈ 2μs each)
    # Two-sided RPC ≈ 2x one-sided (send + recv + processing)
    rdma_rpc_ns = RDMA_LATENCY_NS * 2
    rdma_time = (reads + writes) * RDMA_LATENCY_NS + atomics * RDMA_LATENCY_NS + twosided * rdma_rpc_ns

    per_thread_time = (threads / paper_mops) * 1000  # ns per op (total across threads)
    # More precisely: throughput = threads / per_op_time_per_thread
    # So per_op_time_per_thread = threads / throughput_ops_per_ns
    per_op_ns = (threads / (paper_mops * 1e6)) * 1e9  # ns per op per thread

    compute_time = max(per_op_ns - rdma_time, 50)  # floor at 50ns to avoid negative

    # CXL projection
    # No two-sided RPC under CXL (passive expanders → no offloading)
    cxl_time = (reads + writes) * cxl_read_ns + atomics * cxl_cas_ns

    projected_per_op = compute_time + cxl_time
    projected_mops = threads / (projected_per_op * 1e-9) / 1e6

    speedup = per_op_ns / projected_per_op if projected_per_op > 0 else float("inf")
    rdma_fraction = rdma_time / per_op_ns if per_op_ns > 0 else 0

    return {
        "name": name,
        "workload": workload,
        "rdma_ops": reads + writes + atomics + twosided,
        "rdma_time_ns": rdma_time,
        "compute_time_ns": compute_time,
        "per_op_ns": per_op_ns,
        "rdma_fraction": rdma_fraction,
        "cxl_time_ns": cxl_time,
        "projected_per_op_ns": projected_per_op,
        "paper_mops": paper_mops,
        "projected_mops": projected_mops,
        "speedup": speedup,
    }


# ═══════════════════════════════════════════════════════════════════
# Report generation
# ═══════════════════════════════════════════════════════════════════

def generate_report(results_dir, pairs, nps_mode, topo, local_ns, remote_ns):
    """Generate the full markdown projection document."""
    numa_penalty = remote_ns - local_ns if (remote_ns and local_ns) else 0
    lines = []
    w = lines.append

    w("# DEX CXL Projection: Measured NUMA Latencies → Estimated Throughput\n")
    w(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    w(f"Source data: `{results_dir}`\n")

    # ── Section 1: Measured Environment ──
    w("## 1. Measured Environment\n")
    w(f"- **NPS mode**: {nps_mode}")
    w(f"- **NUMA nodes**: {len(topo.get('nodes', {}))}")
    for nid, size_mb in sorted(topo.get("nodes", {}).items()):
        w(f"  - Node {nid}: {size_mb} MB")
    w("")

    if topo.get("distances"):
        w("**NUMA distance matrix** (from `numactl --hardware`):\n")
        nodes = sorted(topo["nodes"].keys())
        header = "| | " + " | ".join(f"Node {n}" for n in nodes) + " |"
        sep = "|---|" + "|".join(["---:"] * len(nodes)) + "|"
        w(header)
        w(sep)
        for i, row in enumerate(topo["distances"]):
            w(f"| **Node {nodes[i]}** | " + " | ".join(str(d) for d in row) + " |")
        w("")

    w("**Pairwise idle latency** (MLC pointer chase, Phase 4):\n")
    all_nodes = sorted(set(s for s, d in pairs.keys()) | set(d for s, d in pairs.keys()))
    header = "| | " + " | ".join(f"→ Node {n}" for n in all_nodes) + " |"
    sep = "|---|" + "|".join(["---:"] * len(all_nodes)) + "|"
    w(header)
    w(sep)
    for src in all_nodes:
        cells = []
        for dst in all_nodes:
            v = pairs.get((src, dst))
            if v is not None:
                cells.append(f"{v:.1f} ns")
            else:
                cells.append("—")
        w(f"| **CPU on {src}** | " + " | ".join(cells) + " |")
    w("")

    w(f"**Derived values** (min-local, max-remote — see note):\n")
    w(f"- Local DRAM latency (min of src==dst): **{local_ns:.1f} ns**")
    w(f"- Remote NUMA latency (max of src!=dst): **{remote_ns:.1f} ns**")
    w(f"- Cross-domain penalty (Δ): **{numa_penalty:.1f} ns**")
    w(f"- Remote/local ratio: **{remote_ns/local_ns:.2f}×**\n" if local_ns else "")
    w("> **Why min/max instead of averages?** Under NPS4, the NUMA domains are not")
    w("> symmetric — each CCD has a different distance to its memory controller,")
    w("> producing a gradient of latencies (e.g., 110→135 ns) rather than a clean")
    w("> local/remote split. Averaging collapses this gradient and can yield a near-")
    w("> zero cross-domain penalty. Min-local captures the best-case \"local DRAM\"")
    w("> baseline that CXL is compared against, and max-remote captures the worst-case")
    w("> intra-socket NUMA penalty — the natural floor for CXL emulation.\n")

    # ── Section 2: CXL Latency Mapping ──
    cxl_points = compute_cxl_latencies(local_ns, remote_ns)

    w("## 2. CXL Latency Mapping\n")
    w("CXL latency numbers are drawn from published measurements on real hardware.")
    w("Some sources report **ratios** relative to local DDR (these scale with your")
    w("hardware), while others report **absolute** latencies for specific products")
    w("(these are fixed device properties). The \"Basis\" column shows which.\n")

    w("| Label | Basis | Projected latency | Models | Source |")
    w("|-------|-------|------------------:|--------|--------|")
    for label, lat, basis, desc, source in cxl_points:
        inj = lat - remote_ns
        inj_str = f"(inject +{inj:.0f} ns)" if inj > 0 else "(native)"
        w(f"| `{label}` | {basis} | **{lat:.0f} ns** {inj_str} | {desc} | {source} |")
    w("")

    w(f"For comparison, RDMA READ latency (ConnectX-5, InfiniBand): **~{RDMA_LATENCY_NS} ns**")
    w(f"({RDMA_LATENCY_NS / local_ns:.0f}× local DRAM)\n" if local_ns else "\n")

    # ── Section 3: Throughput Projection ──
    w("## 3. Throughput Projection\n")
    w("### Method\n")
    w("From the DEX paper (Table 2), we know RDMA operations per index operation for each")
    w("system. We decompose per-thread time into:\n")
    w("```")
    w("per_op_time = rdma_time + compute_time")
    w("rdma_time   = (reads + writes) × 2000ns + atomics × 2000ns")
    w("compute_time = per_op_time − rdma_time  (held constant under CXL)")
    w("```\n")
    w("Under CXL, we replace the RDMA latency with the measured/projected CXL latency.")
    w("Two-sided RPC (offloading) is removed because real CXL expanders are passive.\n")

    for workload in ["read-only", "write-intensive"]:
        w(f"### {workload.replace('-', ' ').title()} (skewed, 144 threads)\n")

        # Decomposition table
        w("**Latency decomposition (RDMA baseline)**:\n")
        w("| | " + " | ".join(TABLE2.keys()) + " |")
        w("|---|" + "|".join(["---:"] * len(TABLE2)) + "|")

        # Row: RDMA ops/op
        row_ops = []
        for name in TABLE2:
            r, wr, a, ts, _, _ = TABLE2[name][workload]
            row_ops.append(f"{r + wr + a + ts:.2f}")
        w("| RDMA ops / op | " + " | ".join(row_ops) + " |")

        # Row: paper throughput
        row_mops = [f"{TABLE2[n][workload][4]:.1f}" for n in TABLE2]
        w("| Paper throughput (Mops/s) | " + " | ".join(row_mops) + " |")

        # Row: RDMA time
        projs_rdma = {n: project_system(n, workload, remote_ns, remote_ns) for n in TABLE2}
        row_rdma = [f"{projs_rdma[n]['rdma_time_ns']:.0f}" for n in TABLE2]
        w("| RDMA time (ns/op) | " + " | ".join(row_rdma) + " |")

        # Row: compute time
        row_compute = [f"{projs_rdma[n]['compute_time_ns']:.0f}" for n in TABLE2]
        w("| Compute time (ns/op) | " + " | ".join(row_compute) + " |")

        # Row: RDMA fraction
        row_frac = [f"{projs_rdma[n]['rdma_fraction']:.0%}" for n in TABLE2]
        w("| RDMA fraction | " + " | ".join(row_frac) + " |")
        w("")

        # CXL projection table
        w("**Projected throughput under CXL**:\n")
        cxl_header = "| CXL scenario | Total latency | " + " | ".join(TABLE2.keys()) + " |"
        cxl_sep = "|---|---:|" + "|".join(["---:"] * len(TABLE2)) + "|"
        w(cxl_header)
        w(cxl_sep)

        for label, cxl_lat, _, _, _ in cxl_points:
            # For atomics, use same latency (CAS ≈ load/store on same NUMA node)
            cxl_cas = cxl_lat  # conservative estimate
            cells = []
            for name in TABLE2:
                p = project_system(name, workload, cxl_lat, cxl_cas)
                cells.append(f"{p['projected_mops']:.1f} ({p['speedup']:.2f}×)")
            w(f"| `{label}` | {cxl_lat:.0f} ns | " + " | ".join(cells) + " |")

        # RDMA baseline row for comparison
        cells_rdma = [f"{TABLE2[n][workload][4]:.1f} (1.00×)" for n in TABLE2]
        w(f"| RDMA baseline | {RDMA_LATENCY_NS} ns | " + " | ".join(cells_rdma) + " |")
        w("")

    # ── Section 4: Competitive Gap Analysis ──
    w("## 4. Competitive Gap Analysis\n")
    w("The key question: does DEX's advantage over Sherman shrink under CXL?\n")

    w("| Scenario | DEX (Mops/s) | Sherman (Mops/s) | DEX/Sherman ratio |")
    w("|----------|-------------:|-----------------:|------------------:|")

    # RDMA baseline
    w(f"| RDMA (paper) | {TABLE2['DEX']['read-only'][4]:.1f} | {TABLE2['Sherman']['read-only'][4]:.1f} "
      f"| {TABLE2['DEX']['read-only'][4] / TABLE2['Sherman']['read-only'][4]:.2f}× |")

    for label, cxl_lat, _, _, _ in cxl_points[:3]:  # just first three
        p_dex = project_system("DEX", "read-only", cxl_lat, cxl_lat)
        p_sher = project_system("Sherman", "read-only", cxl_lat, cxl_lat)
        ratio = p_dex["projected_mops"] / p_sher["projected_mops"]
        w(f"| CXL `{label}` | {p_dex['projected_mops']:.1f} | {p_sher['projected_mops']:.1f} | {ratio:.2f}× |")
    w("")

    w("Under RDMA, DEX's optimizations are worth a ~3.5× advantage. Under CXL, the gap")
    w("narrows because Sherman's bottleneck (expensive RDMA reads) largely disappears.")
    w("DEX's optimizations still help, but the marginal value drops from \"essential\" to")
    w("\"incremental.\"\n")

    # ── Section 5: Implications ──
    w("## 5. Implications for DEX's Three Pillars\n")

    # Use the cxl-asic point as reference
    cxl_asic_lat = local_ns * 2.18 if local_ns else 200  # [A] CXL-DMSim ASIC multiplier

    w(f"Using the `cxl-asic` scenario ({cxl_asic_lat:.0f} ns, 2.18× local DRAM per CXL-DMSim) "
      f"as the reference point:\n")

    w("### Logical Partitioning\n")
    w(f"- RDMA CAS cost: ~{RDMA_LATENCY_NS} ns → CXL CPU CAS cost: ~{cxl_asic_lat:.0f} ns")
    w(f"- Speedup on atomics: **{RDMA_LATENCY_NS / cxl_asic_lat:.1f}×** cheaper")
    w("- Partitioning exists to avoid cross-server RDMA CAS. At this cost reduction,")
    w("  shared-node locking is cheap enough that the partitioning overhead (load")
    w("  imbalance, repartitioning cost) may not be justified.\n")

    w("### Cooling Map Cache\n")
    w(f"- DRAM-to-RDMA gap: ~{RDMA_LATENCY_NS / local_ns:.0f}× (drove 18× replacement frequency argument)" if local_ns else "")
    w(f"- DRAM-to-CXL gap: ~{cxl_asic_lat / local_ns:.1f}×" if local_ns else "")
    w("- At a 2–3× gap, cache miss penalty is low enough that a simple LRU or CLOCK")
    w("  may perform within 10% of the cooling map's bucket-level FIFO.\n")

    w("### Offloading\n")
    w("- Real CXL memory expanders are passive (no CPU) → offloading is impossible.")
    w("- Even with hypothetical memory-side compute, the one-sided path (direct load/store)")
    w(f"  at {cxl_asic_lat:.0f} ns is fast enough that the RPC overhead of offloading rarely wins.")
    w("- The DEX paper's offloading cost model: `l_p < (L+1) × (l_o + l_s) × c`.")
    w(f"  Substituting l_o = {cxl_asic_lat:.0f} ns (was 2000 ns) makes the inequality almost never satisfied.\n")

    # ── Section 6: Caveats ──
    w("## 6. Caveats\n")
    w("This is an upper-bound analytical model. Key limitations:\n")
    w("1. **Compute time is assumed constant.** In reality, removing RDMA may shift the")
    w("   bottleneck to CPU cache contention, memory bandwidth, or lock contention.")
    w("2. **NUMA ≠ CXL.** Intra-socket NUMA shares L3 cache across domains; real CXL does not.")
    w("   This makes our emulation slightly faster than real CXL for L3-friendly workloads.")
    w("3. **Bandwidth is not modeled.** CXL devices have constrained bandwidth (20–80% of local DDR).")
    w("   Scan-intensive workloads would be affected more than this model predicts.")
    w("4. **Contention dynamics change.** Sherman's CAS retry storms under RDMA resolve")
    w("   differently under CXL — contention shifts from network to CPU cache lines,")
    w("   which may be better or worse depending on core count and LLC topology.")
    w("5. **Cache hit rate may change.** Lower miss penalty under CXL could make aggressive")
    w("   caching less important, changing DEX's behavior in ways this model doesn't capture.\n")

    # ── Section 7: Next Steps ──
    w("## 7. Next Steps\n")
    w("1. **Run the POC** (`cxl_poc.cpp`) to get real traversal-level measurements and validate this model.")
    cxl_asic_inj = local_ns * 2.18 - remote_ns if local_ns else 70
    cxl_fpga_inj = local_ns * 2.88 - remote_ns if local_ns else 150
    w(f"2. **Sweep delay injection** — use `--delay {max(0,cxl_asic_inj):.0f}` for ASIC CXL "
      f"and `--delay {max(0,cxl_fpga_inj):.0f}` for FPGA CXL (computed from multipliers above).")
    w("3. **Build the shim layer** (Approach A from the design doc) to get real DEX-on-CXL numbers.")
    w("4. **Ablation study**: measure DEX with partitioning/caching/offloading individually disabled")
    w("   under CXL to quantify each pillar's residual value.\n")

    # ── Section 8: References ──
    w("## 8. CXL Latency Sources\n")
    w("The CXL latency sweep points in this report are derived from the following published measurements:\n")
    w("| Key | Paper | Measurement | Value | How used |")
    w("|-----|-------|-------------|-------|----------|")
    w("| [A] | CXL-DMSim (2024), arXiv:2411.02282 | ASIC CXL expander vs local DDR | 2.18× local DDR | **Ratio** — scaled by your measured local DRAM |")
    w("| [A] | CXL-DMSim (2024), arXiv:2411.02282 | FPGA CXL expander vs local DDR | 2.88× local DDR | **Ratio** — scaled by your measured local DRAM |")
    w("| [B] | SupMario / Liu et al. (2024), arXiv:2409.14317 | 4 real CXL devices, locally-attached | 200–400 ns | **Absolute** — 400ns used for cxl-switch |")
    w("| [B] | SupMario / Liu et al. (2024), arXiv:2409.14317 | CXL with switch | ~600 ns | **Absolute** |")
    w("| [C] | Samsung CMM-B spec (Memcon 2024) | 8-device pooled CXL box | 596 ns | **Absolute** — device property, not system-dependent |")
    w("| [D] | M²NDP (2024), arXiv:2404.19381 | Optimized ASIC load-to-use | 150–175 ns | Reference only (confirms [A] range) |")
    w("| [E] | CXLAimPod (2025), arXiv:2508.15980 | CXL vs DDR5 | 130–200 ns vs 75–85 ns | Reference only (confirms [A] range) |")
    w("")
    w("**DEX paper**: Lu et al., \"DEX: Scalable Range Indexing on Disaggregated Memory,\" PVLDB 17(10), 2024.")
    w("RDMA latency (~2000 ns) from Section 2.2; Table 2 RDMA statistics under 144 threads.\n")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Generate CXL throughput projections from NUMA latency measurements"
    )
    parser.add_argument("results_dir", help="Path to measure_numa_latency.sh output directory")
    parser.add_argument("--output", "-o", default=None,
                        help="Output markdown file (default: <results_dir>/cxl_projection.md)")
    args = parser.parse_args()

    rdir = Path(args.results_dir)
    if not rdir.is_dir():
        print(f"ERROR: {rdir} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Parse measurement files
    pairwise_file = rdir / "pairwise_latency.txt"
    if not pairwise_file.exists():
        print(f"ERROR: {pairwise_file} not found. Run measure_numa_latency.sh first.", file=sys.stderr)
        sys.exit(1)

    pairs = parse_pairwise(pairwise_file)
    nps_mode = parse_nps_mode(rdir / "nps_mode.txt")
    topo = parse_topology(rdir / "topology.txt")
    local_ns, remote_ns = get_local_remote_latency(pairs)

    if local_ns is None or remote_ns is None:
        print("ERROR: Could not extract local/remote latencies from pairwise data.", file=sys.stderr)
        print(f"  Parsed pairs: {pairs}", file=sys.stderr)
        sys.exit(1)

    print(f"NPS mode:       {nps_mode}")
    print(f"Local latency:  {local_ns:.1f} ns (min of src==dst pairs)")
    print(f"Remote latency: {remote_ns:.1f} ns (max of src!=dst pairs)")
    print(f"NUMA penalty:   {remote_ns - local_ns:.1f} ns")
    print(f"Ratio:          {remote_ns / local_ns:.2f}×")

    # Generate report
    report = generate_report(str(rdir), pairs, nps_mode, topo, local_ns, remote_ns)

    out_path = args.output or str(rdir / "cxl_projection.md")
    with open(out_path, "w") as f:
        f.write(report)

    print(f"\nReport written to: {out_path}")


if __name__ == "__main__":
    main()