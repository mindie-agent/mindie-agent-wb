"""Thin resident WorkBuddy MCP delegator over `mindie_harness` (`< 50` lines).
[Role] Serves disjoint `remote` (15-tool `wb` profile), `tools`, and `knowledge` surfaces.
[Invariants] Zero pre-call session binding; `tools` omits `knowledge_*` to avoid 3-server duplication.
[Timeouts & Locks] Concurrency, framing, and deadlines are owned by `mindie_harness`.
[Side-Effect Policy] `tools/list` creates zero SQLite; `tools/call` delegates to `ManagedMCPAdapter`.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys
from mindie_harness.api import ProfileRef, serve_mcp
from mindie_harness.mcp.gateway import ManagedMCPAdapter, rebind_native_plugin_if_needed
from mindie_harness.mcp.knowledge_schemas import KNOWLEDGE_NAMES
from bridge import knowledge_transport
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
class WBManagedAdapter(ManagedMCPAdapter):
    def __init__(self, surface: str, **kwargs) -> None:
        super().__init__("wb", include_knowledge=False if surface in {"tools", "components"} else None, **kwargs)
    def mcp_tools(self, surface, profile):
        if str(surface).lower() == "remote": os.environ["REMOTE_DEV_HARNESS_PROFILE"] = "wb"
        return super().mcp_tools(surface, profile)
    def dispatch_tool(self, ctx, arguments, profile):
        if ctx.tool_name.startswith("remote_"): ctx.env["REMOTE_DEV_HARNESS_PROFILE"] = "wb"
        result = super().dispatch_tool(ctx, arguments, profile)
        if ctx.tool_name in KNOWLEDGE_NAMES and (result.get("structuredContent") or {}).get("reason") == "knowledge_unavailable":
            return knowledge_transport(ctx, dict(arguments or {}), profile)
        return result
def serve(surface: str, *, config: str | Path | None = None) -> int:
    clean = "tools" if surface == "components" else str(surface or "tools")
    if clean not in {"remote", "tools", "knowledge", "all"}: raise ValueError(f"unknown MindIE MCP surface: {surface}")
    profile = ProfileRef.resolve("wb", plugin_root=PLUGIN_ROOT, config_path=config)
    os.environ.update(REMOTE_DEV_STATE_DIR=str(profile.state_dir / "remote"), REMOTE_DEV_HARNESS_PROFILE="wb")
    try: rebind_native_plugin_if_needed(profile)
    except TimeoutError: raise
    except Exception: pass
    return serve_mcp(clean, profile=profile, adapter=WBManagedAdapter(clean, plugin_root=PLUGIN_ROOT, config_path=profile.config_path))
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    config = argv[argv.index("--config") + 1] if "--config" in argv and argv.index("--config") + 1 < len(argv) else None
    surface = next((item for item in argv if not item.startswith("-") and item != config and item != "--config"), "tools")
    return serve(surface, config=config)
if __name__ == "__main__":
    raise SystemExit(main())
