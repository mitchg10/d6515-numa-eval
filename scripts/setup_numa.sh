#!/bin/bash
# Bootstrap a d6515 node for NUMA latency characterization.
# Runs once on boot via CloudLab execute service.

set -euo pipefail
mkdir -p /local/logs
exec > >(tee -a /local/logs/setup_numa.log) 2>&1

echo "=== NUMA Setup: $(hostname) — $(date) ==="

echo "Installing tools..."
# CloudLab Utah nodes cannot reach us.archive.ubuntu.com (IPv6 unreachable,
# IPv4 times out). Strip the regional prefix so apt resolves to a different
# CDN endpoint, force IPv4, and cap per-connection timeout so we never hang.
sudo sed -i \
    -e 's|http://us\.archive\.ubuntu\.com/ubuntu|http://archive.ubuntu.com/ubuntu|g' \
    -e 's|http://security\.ubuntu\.com/ubuntu|http://archive.ubuntu.com/ubuntu|g' \
    /etc/apt/sources.list
APT_OPTS="-o Acquire::ForceIPv4=true -o Acquire::http::Timeout=20 -o Acquire::Retries=2"
sudo apt-get $APT_OPTS update -qq
sudo apt-get $APT_OPTS install -y -qq \
    numactl lmbench linux-tools-common cpufrequtils hwloc \
    build-essential libnuma-dev

# lmbench installs to /usr/lib/lmbench/bin, not PATH — symlink what we need
sudo ln -sf /usr/lib/lmbench/bin/lat_mem_rd /usr/local/bin/lat_mem_rd 2>/dev/null || true

echo "Setting CPU governor to performance..."
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    echo performance | sudo tee "$cpu" > /dev/null 2>&1 || true
done

echo "Disabling transparent hugepages..."
echo never | sudo tee /sys/kernel/mm/transparent_hugepage/enabled > /dev/null

echo "Disabling ASLR..."
echo 0 | sudo tee /proc/sys/kernel/randomize_va_space > /dev/null

echo "Extracting MLC..."
mkdir -p ~/mlc
# MLC is staged in CloudLab project storage (not the repo — Intel copyright).
# Upload once: scp "Intel Memory Latency Checker v3.12.tgz" <user>@users.cloudlab.us:/proj/<project>/
MLC_TARBALL=$(find /proj -name "Intel Memory Latency Checker v3.12.tgz" 2>/dev/null | head -1 || true)
if [ -z "$MLC_TARBALL" ]; then
    echo "ERROR: MLC tarball not found under /proj. Upload it once with:" >&2
    echo "  scp 'Intel Memory Latency Checker v3.12.tgz' <user>@users.cloudlab.us:/proj/<project>/" >&2
    exit 1
fi
tar -xzf "$MLC_TARBALL" -C ~/mlc --strip-components=1 || \
    tar -xzf "$MLC_TARBALL" -C ~/mlc
chmod +x ~/mlc/mlc 2>/dev/null || true
echo "  MLC binary: $(ls ~/mlc/mlc 2>/dev/null || echo 'NOT FOUND — check strip-components')"

# Detect NPS mode by counting NUMA nodes
NODE_COUNT=$(ls /sys/devices/system/node/ | grep -c "^node[0-9]" || echo 0)
echo ""
echo "=== NPS Detection ==="
echo "NUMA nodes detected: $NODE_COUNT"
case "$NODE_COUNT" in
    1) echo "  NPS1 — single domain, no cross-NUMA penalty. Reboot with NPS2 or NPS4 in BIOS." ;;
    2) echo "  NPS2 — 2 NUMA domains detected." ;;
    4) echo "  NPS4 — 4 NUMA domains detected (optimal for CXL emulation baseline)." ;;
    *) echo "  Unexpected node count: $NODE_COUNT" ;;
esac

echo ""
echo "=== Topology ==="
numactl --hardware
echo ""
lscpu | grep -i numa || true
echo ""
lstopo --of ascii 2>/dev/null || echo "(lstopo ascii unavailable)"

echo ""
echo "=== NUMA Setup complete: $(date) ==="
