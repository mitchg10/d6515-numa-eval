#!/usr/bin/env python3
"""
Parse NUMA latency results from measure_numa_latency.sh output.

Usage:
    python3 analysis/parse_numa_results.py <results_dir>
    python3 analysis/parse_numa_results.py <results_dir> --csv out.csv
"""

import argparse
import csv
import os
import re
import sys

import numpy as np


# ---------------------------------------------------------------------------
# MLC matrix parser
# ---------------------------------------------------------------------------

def _parse_mlc_matrix(path: str) -> np.ndarray | None:
    """Parse an MLC NUMA matrix from idle_latency or loaded_latency output.

    MLC prints matrices like:
        Numa node
        Numa node          0       1       2       3
               0         87.5   208.3   214.1   211.2
               1        209.4    87.8   212.5   210.6
               ...
    Returns a 2-D float array, or None if the file is missing/unparseable.
    """
    if not os.path.exists(path):
        return None

    with open(path) as f:
        lines = f.readlines()

    # Find the header line ("Numa node   0   1   ...")
    header_idx = None
    for i, line in enumerate(lines):
        if re.search(r'Numa\s+node\s+\d', line):
            header_idx = i
            break
    if header_idx is None:
        return None

    rows = []
    for line in lines[header_idx + 1:]:
        # Data rows start with an integer row index
        m = re.match(r'^\s*(\d+)\s+([\d\s.]+)', line)
        if m:
            values = [float(v) for v in m.group(2).split()]
            if values:
                rows.append(values)

    if not rows:
        return None

    # Pad ragged rows to square (MLC sometimes omits trailing zeros)
    n = max(len(r) for r in rows)
    matrix = np.zeros((len(rows), n))
    for i, row in enumerate(rows):
        matrix[i, :len(row)] = row
    return matrix


def _parse_bandwidth_matrix(path: str) -> np.ndarray | None:
    """Parse MLC max_bandwidth output (same matrix format as latency)."""
    return _parse_mlc_matrix(path)


def _parse_pairwise(path: str) -> dict[tuple[int, int], float]:
    """Parse pairwise_latency.txt: lines of 'src dst latency_ns'."""
    result: dict[tuple[int, int], float] = {}
    if not os.path.exists(path):
        return result
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line.startswith('#') or not line:
                continue
            parts = line.split()
            if len(parts) >= 3:
                try:
                    src, dst = int(parts[0]), int(parts[1])
                    lat = float(parts[2])
                    result[(src, dst)] = lat
                except ValueError:
                    pass
    return result


def _parse_nps_mode(results_dir: str) -> str:
    path = os.path.join(results_dir, 'nps_mode.txt')
    if not os.path.exists(path):
        return 'UNKNOWN'
    with open(path) as f:
        for line in f:
            m = re.match(r'NPS_MODE=(\S+)', line)
            if m:
                return m.group(1)
    return 'UNKNOWN'


def _parse_topology(results_dir: str) -> dict:
    path = os.path.join(results_dir, 'topology.txt')
    info: dict = {'num_nodes': None, 'mem_per_node_gb': []}
    if not os.path.exists(path):
        return info

    with open(path) as f:
        content = f.read()

    # "available: 4 nodes (0-3)"
    m = re.search(r'available:\s*(\d+)\s+node', content)
    if m:
        info['num_nodes'] = int(m.group(1))

    # "node 0 size: 32127 MB"
    for m in re.finditer(r'node\s+\d+\s+size:\s*(\d+)\s*MB', content):
        info['mem_per_node_gb'].append(round(int(m.group(1)) / 1024, 1))

    return info


# ---------------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------------

def _matrix_metrics(matrix: np.ndarray) -> dict:
    n = matrix.shape[0]
    diag = [matrix[i, i] for i in range(n) if matrix[i, i] > 0]
    offdiag = [matrix[i, j] for i in range(n) for j in range(n)
               if i != j and matrix[i, j] > 0]

    local = float(np.mean(diag)) if diag else None
    remote = float(np.mean(offdiag)) if offdiag else None
    delta = (remote - local) if (local and remote) else None
    ratio = (remote / local) if (local and remote and local > 0) else None

    return {
        'local_lat_ns': local,
        'remote_lat_ns': remote,
        'delta_ns': delta,
        'ratio': ratio,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_results(results_dir: str) -> dict:
    nps_mode = _parse_nps_mode(results_dir)
    topology = _parse_topology(results_dir)

    idle_matrix = _parse_mlc_matrix(os.path.join(results_dir, 'idle_latency_matrix.txt'))
    loaded_matrix = _parse_mlc_matrix(os.path.join(results_dir, 'loaded_latency_matrix.txt'))
    bw_matrix = _parse_bandwidth_matrix(os.path.join(results_dir, 'bandwidth_matrix.txt'))
    pairwise = _parse_pairwise(os.path.join(results_dir, 'pairwise_latency.txt'))

    idle_metrics = _matrix_metrics(idle_matrix) if idle_matrix is not None else {}
    loaded_metrics = _matrix_metrics(loaded_matrix) if loaded_matrix is not None else {}

    # Per-node peak bandwidth (diagonal of bandwidth matrix, in MB/s)
    bw_per_node: list[float] = []
    if bw_matrix is not None:
        n = bw_matrix.shape[0]
        bw_per_node = [float(bw_matrix[i, i]) for i in range(n) if bw_matrix[i, i] > 0]

    # Pairwise summary
    local_pairs = {k: v for k, v in pairwise.items() if k[0] == k[1]}
    remote_pairs = {k: v for k, v in pairwise.items() if k[0] != k[1]}
    pw_local = float(np.mean(list(local_pairs.values()))) if local_pairs else None
    pw_remote = float(np.mean(list(remote_pairs.values()))) if remote_pairs else None
    pw_ratio = (pw_remote / pw_local) if (pw_local and pw_remote and pw_local > 0) else None

    return {
        'results_dir': results_dir,
        'nps_mode': nps_mode,
        'num_nodes': topology.get('num_nodes'),
        'mem_per_node_gb': topology.get('mem_per_node_gb', []),
        'idle': idle_metrics,
        'loaded': loaded_metrics,
        'bw_per_node_mbs': bw_per_node,
        'pairwise_local_ns': pw_local,
        'pairwise_remote_ns': pw_remote,
        'pairwise_ratio': pw_ratio,
        'raw': {
            'idle_matrix': idle_matrix,
            'loaded_matrix': loaded_matrix,
            'bw_matrix': bw_matrix,
        },
    }


def _fmt(val, precision=1) -> str:
    if val is None:
        return 'N/A'
    return f'{val:.{precision}f}'


def print_summary(r: dict, cxl_low: float = 2.4, cxl_high: float = 2.6) -> None:
    idle = r['idle']
    loaded = r['loaded']

    def ratio_tag(ratio):
        if ratio is None:
            return ''
        if cxl_low <= ratio <= cxl_high:
            return f'  ✓ in CXL target [{cxl_low}–{cxl_high}×]'
        elif ratio > cxl_high:
            return f'  (above CXL target; still usable)'
        else:
            return f'  ✗ below CXL target [{cxl_low}–{cxl_high}×]'

    print()
    print(f'=== NUMA Latency Summary ({r["nps_mode"]}) ===')
    print(f'{"Results dir":<14}: {r["results_dir"]}')
    print(f'{"NPS mode":<14}: {r["nps_mode"]}  ({r["num_nodes"]} NUMA domains)')
    if r['mem_per_node_gb']:
        print(f'{"Mem/node":<14}: {r["mem_per_node_gb"]} GB')
    print()
    print(f'{"Source":<12}  {"Local (ns)":<12} {"Remote (ns)":<13} {"Delta (ns)":<12} {"Ratio":<8}')
    print('-' * 65)

    idle_ratio = idle.get('ratio')
    loaded_ratio = loaded.get('ratio')

    print(f'{"MLC idle":<12}  {_fmt(idle.get("local_lat_ns")):<12} '
          f'{_fmt(idle.get("remote_lat_ns")):<13} '
          f'{_fmt(idle.get("delta_ns")):<12} '
          f'{_fmt(idle_ratio, 2):<8}'
          f'{ratio_tag(idle_ratio)}')

    print(f'{"MLC loaded":<12}  {_fmt(loaded.get("local_lat_ns")):<12} '
          f'{_fmt(loaded.get("remote_lat_ns")):<13} '
          f'{_fmt(loaded.get("delta_ns")):<12} '
          f'{_fmt(loaded_ratio, 2):<8}'
          f'{ratio_tag(loaded_ratio)}')

    if r['pairwise_ratio'] is not None:
        print(f'{"Pairwise":<12}  {_fmt(r["pairwise_local_ns"]):<12} '
              f'{_fmt(r["pairwise_remote_ns"]):<13} '
              f'{"N/A":<12} '
              f'{_fmt(r["pairwise_ratio"], 2):<8}'
              f'{ratio_tag(r["pairwise_ratio"])}')

    if r['bw_per_node_mbs']:
        print()
        print('Peak bandwidth per NUMA domain (MB/s):')
        for i, bw in enumerate(r['bw_per_node_mbs']):
            print(f'  node{i}: {bw:,.0f} MB/s')

    print()


def write_csv(r: dict, csv_path: str) -> None:
    idle = r['idle']
    loaded = r['loaded']
    row = {
        'results_dir': r['results_dir'],
        'nps_mode': r['nps_mode'],
        'num_nodes': r['num_nodes'],
        'idle_local_ns': idle.get('local_lat_ns'),
        'idle_remote_ns': idle.get('remote_lat_ns'),
        'idle_delta_ns': idle.get('delta_ns'),
        'idle_ratio': idle.get('ratio'),
        'loaded_local_ns': loaded.get('local_lat_ns'),
        'loaded_remote_ns': loaded.get('remote_lat_ns'),
        'loaded_delta_ns': loaded.get('delta_ns'),
        'loaded_ratio': loaded.get('ratio'),
        'pairwise_local_ns': r.get('pairwise_local_ns'),
        'pairwise_remote_ns': r.get('pairwise_remote_ns'),
        'pairwise_ratio': r.get('pairwise_ratio'),
    }
    with open(csv_path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)
    print(f'CSV written to: {csv_path}')


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Parse NUMA latency results from measure_numa_latency.sh')
    parser.add_argument('results_dir', help='Path to timestamped results directory')
    parser.add_argument('--csv', help='Write summary to CSV file')
    parser.add_argument('--cxl-target-low', type=float, default=2.4,
                        help='Lower bound of CXL ratio target (default: 2.4)')
    parser.add_argument('--cxl-target-high', type=float, default=2.6,
                        help='Upper bound of CXL ratio target (default: 2.6)')
    args = parser.parse_args()

    if not os.path.isdir(args.results_dir):
        print(f'ERROR: results directory not found: {args.results_dir}', file=sys.stderr)
        sys.exit(1)

    results = parse_results(args.results_dir)
    print_summary(results, cxl_low=args.cxl_target_low, cxl_high=args.cxl_target_high)

    if args.csv:
        write_csv(results, args.csv)


if __name__ == '__main__':
    main()
