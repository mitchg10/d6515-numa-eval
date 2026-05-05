# NUMA Latency Characterization on CloudLab d6515

Measures cross-NUMA latency on a single AMD EPYC 7452 (d6515) node to establish
a baseline "emulated CXL penalty." NPS4 splits the socket into 4 sub-NUMA domains;
cross-domain latency targets the ~2.4-2.6x local DDR ratio reported in CXL literature.

## Prerequisites

**One-time: upload MLC to CloudLab project storage**

Intel's Memory Latency Checker cannot be redistributed, so it is not in the repo.
Upload it once to CloudLab's project NFS share (accessible on all nodes at `/proj/<project>/`):

```bash
scp "Intel Memory Latency Checker v3.12.tgz" \
    <username>@users.cloudlab.us:/proj/<project-name>/
```

## Step 1: Configure NPS mode in BIOS

NPS4 is required for the best CXL approximation (4 NUMA domains). NPS2 (2 domains)
also works but gives less data points. NPS1 (default on some images) is unusable for
this experiment.

In the CloudLab experiment panel, request a hardware-specific BIOS setting for your
d6515 node, or contact CloudLab ops to enable NPS4. After rebooting, verify:

```bash
numactl --hardware   # should show 4 nodes for NPS4, 2 for NPS2
```

If you only see 1 node, the experiment will abort with a clear error message.

## Step 2: Submit the CloudLab profile

1. Go to **Experiments > Create Experiment** in the CloudLab portal
2. Select your project
3. Upload or paste `profile.py` as the profile source
4. Click **Instantiate** — this allocates one d6515 node

On first boot, `scripts/setup_numa.sh` runs automatically and:
- Installs `numactl`, `lmbench`, `hwloc`, `cpufrequtils`
- Extracts MLC from `/proj/<project>/`
- Sets CPU governor to `performance`
- Disables transparent hugepages and ASLR
- Prints NPS mode and topology

Monitor boot progress:
```bash
ssh <node>
tail -f /local/logs/setup_numa.log
```

## Step 3: Run measurements

SSH into the node, then:

```bash
bash /local/repository/scripts/measure_numa_latency.sh
```

This runs 6 phases and saves all output to a timestamped directory under `~/numa-results/`:

| Phase | What it measures | Output file |
|-------|-----------------|-------------|
| 0 | System snapshot (topology, lscpu, cpuinfo) | `topology.txt`, `lscpu.txt`, `cpuinfo.txt` |
| 1 | NPS detection — aborts if NPS1 | `nps_mode.txt` |
| 2 | MLC idle latency matrix | `idle_latency_matrix.txt` |
| 3 | MLC loaded latency matrix | `loaded_latency_matrix.txt` |
| 4 | Per-pair numactl + MLC latency | `pairwise_latency.txt` |
| 5 | MLC peak bandwidth matrix | `bandwidth_matrix.txt` |
| 6 | lmbench cross-check (256m, 1g buffers) | `lmbench_local_*.txt`, `lmbench_remote_*.txt` |

The run takes roughly 10-20 minutes on NPS4.

## Step 4: Parse results

```bash
# On the node, or copy results back first:
python3 /local/repository/analysis/parse_numa_results.py ~/numa-results/<timestamp>/

# Optional: write a CSV
python3 /local/repository/analysis/parse_numa_results.py ~/numa-results/<timestamp>/ \
    --csv numa_summary.csv
```

Example output for NPS4:

```
=== NUMA Latency Summary (NPS4) ===
NPS mode      : NPS4  (4 NUMA domains)
Mem/node      : [32.0, 32.0, 32.0, 32.0] GB

Source        Local (ns)   Remote (ns)  Delta (ns)  Ratio
-----------------------------------------------------------------
MLC idle      87.5         208.3        120.8       2.38    in CXL target [2.4-2.6x]
MLC loaded    103.1        247.6        144.5       2.40    in CXL target [2.4-2.6x]
Pairwise      88.2         210.1        N/A         2.38    in CXL target [2.4-2.6x]
```

A ratio >= 2.4x validates the "emulated CXL penalty" argument.

## Step 5: Copy results off the node

```bash
# From your laptop:
scp -r <node>:~/numa-results/ ./numa-results/
```

## Interpreting results

| Ratio | Interpretation |
|-------|---------------|
| < 1.5x | NPS mode likely not set correctly, or measuring L3 not DRAM |
| 1.5-2.0x | NPS2 typical range |
| 2.0-2.6x | NPS4 typical range; >= 2.4x matches CXL literature |
| > 3.0x | Unexpected — check for NUMA balancing interference |

If the ratio is below target, check:
- `cat ~/numa-results/<timestamp>/nps_mode.txt` — confirms NPS mode
- `cat ~/numa-results/<timestamp>/lscpu.txt | grep -i numa` — node count
- Ensure `numactl --hardware` shows distinct memory per node (not interleaved)

## File reference

```
profile.py                        CloudLab profile — allocates one d6515
scripts/setup_numa.sh             Node bootstrap (runs on boot)
scripts/measure_numa_latency.sh   Measurement driver (run manually)
analysis/parse_numa_results.py    Parses results, prints summary, writes CSV
configs/numa_measurement.yaml     Reference parameters and CXL ratio targets
```
