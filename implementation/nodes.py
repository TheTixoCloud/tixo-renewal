"""Multi-node configuration for the renewal system.

Each node is one Virtualizor master. Adding a future node means adding one entry to
nodes.json -- no code changes. Every node gets its own SQLite database file (never shared),
so a mistake on one node's data can never corrupt another node's ledger.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "renewal-config", "nodes.json")


@dataclass(frozen=True)
class NodeConfig:
    node_id: str
    ssh_host: str = ""     # e.g. "root@your-virtualizor-host.example.com" -- unused when mode="local"
    ssh_key: str = ""      # e.g. "~/.ssh/id_virtualizor" -- unused when mode="local"
    db_filename: str = "node.sqlite3"  # relative to shadow-monitor-state/
    mode: str = "ssh"      # "ssh" (dev-server polls a remote host) | "local" (runs ON the
    #                         Virtualizor host itself, reads its own DB directly, no SSH)

    def ssh_cmd(self) -> list[str]:
        if self.mode != "ssh":
            raise ValueError(f"node {self.node_id} is mode={self.mode!r}, has no ssh_cmd")
        return ["ssh", "-i", os.path.expanduser(self.ssh_key), "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", self.ssh_host]

    def db_path(self, state_dir: str) -> str:
        return os.path.join(state_dir, self.db_filename)


# No real host is ever hardcoded here -- this package ships with NO production identity of any
# kind. An operator must always provide their own real node(s) via renewal-config/nodes.json
# (see docs/examples/nodes.example.json). This placeholder is deliberately unusable as-is: its
# ssh_host/ssh_key are empty, so ssh_cmd() will fail loudly rather than silently targeting
# someone's real server by accident.
DEFAULT_NODES = [
    NodeConfig(node_id="unconfigured", ssh_host="", ssh_key="", db_filename="unconfigured.sqlite3", mode="ssh"),
]

# Used by install.sh when deploying directly onto a Virtualizor host: exactly one node,
# representing "this host," reading its own local DB -- no SSH, no other node's data.
LOCAL_DEPLOYMENT_NODE_TEMPLATE = NodeConfig(node_id="self", mode="local", db_filename="self.sqlite3")


def load_nodes(config_path: str = DEFAULT_CONFIG_PATH) -> list[NodeConfig]:
    """Loads the node list from JSON. If the config file doesn't exist yet, returns the
    single known production node as a safe, explicit default (never silently zero nodes)."""
    if not os.path.exists(config_path):
        return list(DEFAULT_NODES)
    data = json.loads(Path(config_path).read_text())
    return [NodeConfig(**n) for n in data["nodes"]]


def write_default_config(config_path: str = DEFAULT_CONFIG_PATH, nodes: list[NodeConfig] = None) -> None:
    """Idempotent: only writes if the file doesn't already exist, so re-running install never
    clobbers an operator's edited node list."""
    if os.path.exists(config_path):
        return
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    nodes = nodes or DEFAULT_NODES
    payload = {"nodes": [
        {"node_id": n.node_id, "ssh_host": n.ssh_host, "ssh_key": n.ssh_key,
         "db_filename": n.db_filename, "mode": n.mode}
        for n in nodes
    ]}
    tmp = config_path + ".tmp"
    Path(tmp).write_text(json.dumps(payload, indent=2) + "\n")
    os.replace(tmp, config_path)
