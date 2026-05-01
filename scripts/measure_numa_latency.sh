#!/bin/bash
# NUMA latency characterization driver for d6515 (AMD EPYC 7452).
# Runs 6 measurement phases and saves all output to a timestamped results dir.
#
# Usage: bash measure_numa_latency.sh [results_base_dir]
#   Default results base: ~/numa-results

set -euo pipefail

MLC=~/mlc/mlc
RESULTS_BASE="${1:-$HOME/numa-results}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="$RESULTS_BASE/$TIMESTAMP"

mkdir -p "$RESULTS_DIR"
exec > >(tee -a "$RESULTS_DIR/run.log") 2>&1

echo "============================================================"
echo "NUMA Latency Characterization — $(hostname)"
echo "Date: $(date)"
echo "Results: $RESULTS_DIR"
echo "============================================================"

# Verify MLC is present
if [ ! -x "$MLC" ]; then
    # Try finding mlc binary anywhere under ~/mlc
    MLC=$(find ~/mlc -name mlc -type f 2>/dev/null | head -1 || true)
    if [ -z "$MLC" ]; then
        echo "ERROR: MLC binary not found. Run setup_numa.sh first." >&2
        exit 1
    fi
    chmod +x "$MLC"
fi
echo "MLC binary: $MLC"

# ----------------------------------------------------------------
# Phase 0 — System snapshot
# ----------------------------------------------------------------
echo ""
echo "--- Phase 0: System snapshot ---"
numactl --hardware                     > "$RESULTS_DIR/topology.txt"
lscpu                                  > "$RESULTS_DIR/lscpu.txt"
cat /proc/cpuinfo                      > "$RESULTS_DIR/cpuinfo.txt"
lstopo --of ascii 2>/dev/null          > "$RESULTS_DIR/topology_ascii.txt" || true
echo "  Saved: topology.txt, lscpu.txt, cpuinfo.txt, topology_ascii.txt"

# ----------------------------------------------------------------
# Phase 1 — Detect NPS setting
# ----------------------------------------------------------------
echo ""
echo "--- Phase 1: NPS detection ---"
NODE_COUNT=$(ls /sys/devices/system/node/ | grep -c "^node[0-9]" || echo 0)
echo "NUMA nodes: $NODE_COUNT" | tee "$RESULTS_DIR/nps_mode.txt"

case "$NODE_COUNT" in
    1)
        echo "WARNING: NPS1 — single NUMA domain. Cross-domain penalty not measurable."
        echo "Suggested fix: reboot node with BIOS NPS4 (or NPS2) setting."
        echo "NPS_MODE=NPS1" >> "$RESULTS_DIR/nps_mode.txt"
        echo "Aborting measurement — no cross-NUMA pairs to characterize." >&2
        exit 1
        ;;
    2)
        echo "NPS2 confirmed — 2 NUMA domains." | tee -a "$RESULTS_DIR/nps_mode.txt"
        NPS_LABEL="NPS2"
        ;;
    4)
        echo "NPS4 confirmed — 4 NUMA domains (optimal)." | tee -a "$RESULTS_DIR/nps_mode.txt"
        NPS_LABEL="NPS4"
        ;;
    *)
        echo "WARNING: Unexpected NUMA node count ($NODE_COUNT). Proceeding anyway."
        NPS_LABEL="NPS_UNKNOWN_${NODE_COUNT}"
        ;;
esac
echo "NPS_MODE=$NPS_LABEL" >> "$RESULTS_DIR/nps_mode.txt"

# Build list of NUMA node indices
NODES=$(seq 0 $((NODE_COUNT - 1)))

# ----------------------------------------------------------------
# Phase 2 — MLC idle latency matrix
# ----------------------------------------------------------------
echo ""
echo "--- Phase 2: MLC idle latency matrix ---"
sudo "$MLC" --latency_matrix -e -r 2>&1 | tee "$RESULTS_DIR/idle_latency_matrix.txt"
echo "  Saved: idle_latency_matrix.txt"

# ----------------------------------------------------------------
# Phase 3 — MLC loaded latency matrix
# ----------------------------------------------------------------
echo ""
echo "--- Phase 3: MLC loaded latency matrix ---"
sudo "$MLC" --loaded_latency -e -r 2>&1 | tee "$RESULTS_DIR/loaded_latency_matrix.txt"
echo "  Saved: loaded_latency_matrix.txt"

# ----------------------------------------------------------------
# Phase 4 — Explicit per-pair numactl + MLC idle latency
# Uses 200 MB buffer to ensure measurements land in DRAM (above L3).
# ----------------------------------------------------------------
echo ""
echo "--- Phase 4: Pairwise numactl latency ---"
PAIRWISE="$RESULTS_DIR/pairwise_latency.txt"
echo "# src_node dst_node latency_ns" > "$PAIRWISE"

for src in $NODES; do
    for dst in $NODES; do
        echo -n "  node$src → node$dst: "
        raw=$(sudo numactl --cpunodebind="$src" --membind="$dst" \
                  "$MLC" --idle_latency -b200m 2>&1 || true)
        result=$(echo "$raw" | awk '/Each iteration/ {print $(NF-1)}')
        [[ -z "$result" ]] && result="N/A"
        echo "$result"
        echo "$src $dst $result" >> "$PAIRWISE"
    done
done
echo "  Saved: pairwise_latency.txt"

# ----------------------------------------------------------------
# Phase 5 — Bandwidth matrix
# ----------------------------------------------------------------
echo ""
echo "--- Phase 5: MLC bandwidth matrix ---"
sudo "$MLC" --max_bandwidth -e 2>&1 | tee "$RESULTS_DIR/bandwidth_matrix.txt"
echo "  Saved: bandwidth_matrix.txt"

# ----------------------------------------------------------------
# Phase 6 — lmbench cross-check (first two nodes only)
# ----------------------------------------------------------------
echo ""
echo "--- Phase 6: lmbench cross-check ---"

# Verify lat_mem_rd is available
if ! command -v lat_mem_rd &>/dev/null; then
    echo "  WARNING: lat_mem_rd not found. Skipping lmbench phase."
else
    for buf in 256m 1g; do
        numactl --cpunodebind=0 --membind=0 \
            lat_mem_rd "$buf" 64 2>&1 > "$RESULTS_DIR/lmbench_local_${buf}.txt"
        echo "  Saved: lmbench_local_${buf}.txt"

        if [ "$NODE_COUNT" -ge 2 ]; then
            numactl --cpunodebind=0 --membind=1 \
                lat_mem_rd "$buf" 64 2>&1 > "$RESULTS_DIR/lmbench_remote_${buf}.txt"
            echo "  Saved: lmbench_remote_${buf}.txt"
        fi
    done
fi

# ----------------------------------------------------------------
# Summary
# ----------------------------------------------------------------
echo ""
echo "============================================================"
echo "All phases complete."
echo "NPS mode : $NPS_LABEL"
echo "Results  : $RESULTS_DIR"
echo "Next step: python3 analysis/parse_numa_results.py $RESULTS_DIR"
echo "============================================================"
