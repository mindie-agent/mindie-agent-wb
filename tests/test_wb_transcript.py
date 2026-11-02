"""Tier 0 tests for `wb_transcript.py` on real WorkBuddy SessionStore JSONL records."""

from __future__ import annotations

import json
from pathlib import Path

import wb_transcript


SESSION_ID = "454c6ee9-afea-47be-8188-abb9c7fb1f26"


def _write_transcript_lines(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        for rec in records:
            stream.write((json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))


def test_transcript_extracts_public_messages_from_workbuddy_session_store(tmp_path: Path):
    proj_dir = tmp_path / "workbuddy" / "projects" / "mindie-agent"
    transcript_file = proj_dir / f"{SESSION_ID}.jsonl"

    records = [
        {
            "type": "session-meta",
            "sessionId": SESSION_ID,
            "cwd": str(tmp_path),
            "timestamp": 1761898800000,
        },
        {
            "id": "msg-0",
            "parentId": None,
            "type": "message",
            "timestamp": 1761898800000,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "早期未激活对话\n<system-reminder>\nhidden reminder\n</system-reminder>",
                    }
                ],
            },
        },
        {
            "id": "msg-1",
            "parentId": "msg-0",
            "type": "message",
            "timestamp": 1761899100000,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "/mindie-agent 分析 vLLM-Ascend A3 双 Die Phy-ID 映射规则\n"
                            "<system-reminder>\nInternal hook context\n</system-reminder>"
                        ),
                    }
                ],
            },
        },
        {
            "id": "msg-2",
            "parentId": "msg-1",
            "type": "message",
            "timestamp": 1761899105000,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "SECRET_INTERNAL_CHAIN_OF_THOUGHT_DO_NOT_EXPORT",
                    },
                    {
                        "type": "tool_use",
                        "name": "read_file",
                        "input": {"path": "secret_tool_arg.py"},
                    },
                    {
                        "type": "text",
                        "text": "在昇腾 A3 910C 上，单张物理卡包含 2 个 Die，8 张卡对应 Phy-ID 0..15。",
                    },
                ],
            },
        },
    ]
    _write_transcript_lines(transcript_file, records)

    binding = {
        "session": SESSION_ID,
        "enabled": 1,
        "entry_enabled": 1,
        "project_root": str(tmp_path),
        "activated_at": 1761899000.0,
    }
    source = wb_transcript.capture_source(transcript_file, SESSION_ID, binding=binding)
    assert Path(source["transcript_path"]) == transcript_file.resolve()

    material = wb_transcript.read_material(
        source["transcript_path"],
        0,
        session_id=SESSION_ID,
        not_before=binding["activated_at"],
    )
    assert material["status"] == "ok"
    assert material["records"] == 2
    assert "早期未激活对话" not in material["text"]
    assert "分析 vLLM-Ascend A3 双 Die Phy-ID 映射规则" in material["text"]
    assert "在昇腾 A3 910C 上，单张物理卡包含 2 个 Die" in material["text"]
    assert "SECRET_INTERNAL_CHAIN_OF_THOUGHT_DO_NOT_EXPORT" not in material["text"]
    assert "secret_tool_arg.py" not in material["text"]
    assert "system-reminder" not in material["text"]

    # Incremental read from `material["end"]` with persisted FileIdentity reports unchanged
    persisted = wb_transcript.FileIdentity.unserialize(material["identity"], str(transcript_file.resolve()))
    assert persisted is not None
    inc2 = wb_transcript.read_material(
        source["transcript_path"],
        material["end"],
        session_id=SESSION_ID,
        expected=persisted,
    )
    assert inc2["status"] == "unchanged"
