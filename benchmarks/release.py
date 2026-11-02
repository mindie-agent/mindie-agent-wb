"""[Role] Measure a repeatable public-input component workload for WorkBuddy releases.
[Invariants] Assert business output before reporting latency; no productivity claim.
[Timeouts & Locks] Bounded iterations, no network or background services.
[Side-Effect Policy] Temporary synthetic files and an explicit result JSON only.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "mindie-agent" / "scripts"))
import wb_transcript

session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
records = [
    {
        "type": "message",
        "sessionId": session,
        "role": "assistant",
        "timestamp": "2026-11-02T12:00:00Z",
        "content": [
            {"type": "thinking", "thinking": "HIDDEN_SENTINEL"},
            {"type": "text", "text": "Public NPU mapping observation " + str(i)},
        ],
    }
    for i in range(100)
]
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / (session + ".jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    iterations = 100
    started = time.perf_counter()
    for _ in range(iterations):
        result = wb_transcript.read_material(str(path), 0, session_id=session)
    elapsed = time.perf_counter() - started
    assert result["status"] == "ok" and "observation 99" in result["text"]
    assert "HIDDEN_SENTINEL" not in result["text"]

METRIC = "public_100_message_parse_latency"
Path(os.environ["MINDIE_BENCH_RESULT_PATH"]).write_text(
    json.dumps(
        {
            "status": "pass",
            "metrics": [
                {
                    "name": METRIC,
                    "unit": "us/op",
                    "value": elapsed * 1_000_000 / iterations,
                    "iterations": iterations,
                }
            ],
        }
    )
    + "\n",
    encoding="utf-8",
)
