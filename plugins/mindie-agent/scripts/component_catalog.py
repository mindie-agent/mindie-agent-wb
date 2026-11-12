"""[Role] Describe WorkBuddy's deterministic component CLI capabilities.
[Invariants] Offline analysis and environment inspection require no task lease.
[Timeouts & Locks] Static strings; no waits or locks.
[Side-Effect Policy] Importing this catalog performs no I/O or activation.
"""

COMPONENT_DESCRIPTIONS = {
    "profiler": (
        "Inspect and analyze Ascend NPU profiling captures using mindie-profiler. "
        "Pass CLI arguments as an argv array, e.g. ['inventory', '--root', '/capture'] "
        "or ['analyze', '--root', '/capture', '--full-range']. Also supports correlate, "
        "compare, sql, camodel, and kernels."
        " Offline analysis of existing captures requires no device lease or managed task binding."
    ),
    "sim": (
        "Run explicit-input Ascend NPU performance, Roofline, operator, graph, and "
        "topology/PD Pareto simulations using mindie-sim. Pass CLI arguments as an argv "
        "array; use ['--help'] for supported models, operators, graphs, and serving "
        "calculations."
        " Simulation calculations require no device allocation or managed task binding."
    ),
    "coordinator": (
        "Manage Ascend host inventory, probe CPU/RAM/NPU capacity, bind containers, "
        "and acquire/inspect/release NPU and service-port leases through mindie-coordinator. "
        "Pass CLI argv, e.g. ['hosts', '--action', 'list'] or "
        "['probe', '--json', '{\"action\":\"hosts\"}']. Preserve lease IDs and uncertain "
        "external outcomes; never resubmit an uncertain mutation."
        " Host inventory, probes, containers and bare-metal leases need no managed "
        "task binding or knowledge activation. Omit context_file for these independent "
        "operations; inspect resources before acquiring a lease."
    ),
    "debugger": (
        "Run Ascend NPU CANN fault triage ('triage', 'decode-code'), 3-primitive "
        "storage-graph precision & memory-stomp debugging ('precision' -> inspect-capsule, "
        "replay-call, check-call, compare-traces, slim-moe, watch), ACLGraph capture guard / "
        "address tracker / layer dumper ('aclgraph' -> guard, inspect), and remote NPU "
        "container staging ('remote-guide') through mindie-debugger. Pass CLI arguments as "
        "an argv array, e.g. ['triage', '--file', '/path/to/plog.log'] or "
        "['decode-code', '507011'] or ['aclgraph', 'inspect', '--script', 'repro.py']."
    ),
    "npu_top": (
        "Inspect observed Ascend NPU fleet status, server mounts, and idle NPU capacity "
        "through npu-top CLI ('servers', 'npu', 'status', 'mounts', 'capacity', 'inventory', "
        "'diagnostics'). Observation only; obtain reservations via coordinator."
    ),
}
