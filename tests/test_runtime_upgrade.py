"""Fast generation activation and native-plugin rebinding contracts for `mindie_harness`."""

from __future__ import annotations

import json
from pathlib import Path

from mindie_harness.api import PreparedGeneration, ProfileRef, activate_runtime
from mindie_harness.mcp.gateway import rebind_native_plugin_if_needed


def _generation(profile: ProfileRef, name: str) -> PreparedGeneration:
    generation = profile.generations_dir / name
    generation.mkdir(parents=True, exist_ok=True)
    python = generation / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    receipt = generation / "runtime.json"
    receipt.write_text(json.dumps({
        "schema": "mindie-candidate-check/1", "key": name, "generation": str(generation),
        "python": str(python), "validation": {"status": "validated", "validated_at": 1.0},
    }), encoding="utf-8")
    return PreparedGeneration(status="validated", generation_key=name, generation_dir=generation, python_path=python, receipt_path=receipt, components={})


def test_runtime_generation_activation_and_rebinding(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "scripts").mkdir(parents=True)
    (plugin_root / "plugin.json").write_text(json.dumps({"version": "0.1.19"}), encoding="utf-8")
    profile = ProfileRef.resolve("wb", plugin_root=plugin_root, config_path=tmp_path / "wb.json", data_dir=tmp_path / "data")
    assert not profile.config_path.exists()

    prepared = _generation(profile, "gen-2")
    activated = activate_runtime(profile, prepared, run_gc=False)
    assert activated.status == "active"
    current = json.loads(profile.config_path.read_text(encoding="utf-8"))
    assert current["python"] == str(prepared.python_path)
    assert current["component_release"] == str(prepared.generation_dir)

    current["component_commands"] = {
        "profiler": ["old-python", "-I", "-X", "utf8", "-m", "mindie_profiler"],
        "sim": ["custom-wrapper", "--flag"],
    }
    current["product_validation"] = {"native_version": "0.1.18"}
    profile.config_path.write_text(json.dumps(current), encoding="utf-8")

    rebound = rebind_native_plugin_if_needed(profile)
    assert rebound["status"] == "rebound"
    updated = json.loads(profile.config_path.read_text(encoding="utf-8"))
    assert updated["product_validation"]["native_version"] == "0.1.19"
    assert updated["runtime_scripts"] == str(plugin_root / "scripts")
    assert updated["component_commands"]["profiler"] == [str(prepared.python_path), "-I", "-X", "utf8", "-m", "mindie_profiler"]
    assert updated["component_commands"]["sim"] == ["custom-wrapper", "--flag"]
    assert rebind_native_plugin_if_needed(profile) is None
