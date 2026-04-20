"""
NUMA Characterization Profile: Single d6515 node

Allocates one d6515 (AMD EPYC 7452) for NUMA latency characterization.
No blockstore - MLC lives in the repo. Boots setup_numa.sh to prepare the node.
"""

import geni.portal as portal
import geni.rspec.pg as pg

pc = portal.Context()
request = pc.makeRequestRSpec()

node = request.RawPC("node-0")
node.hardware_type = "d6515"
node.disk_image = "urn:publicid:IDN+emulab.net+image+emulab-ops//UBUNTU22-64-STD"

node.addService(pg.Execute(
    shell="bash",
    command="/local/repository/scripts/setup_numa.sh"))

pc.printRequestRSpec(request)
