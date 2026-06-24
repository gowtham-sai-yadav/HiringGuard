"""End-to-end CLI demo.

Usage:
    python run_demo.py --sample acme_se_role
    python run_demo.py --sample northwind_pm_role --auto-approve
    python run_demo.py --packet path/to/packet.json --decision send_back

Runs the LangGraph pipeline on a sample (or custom) packet. Pauses at the
HITL interrupt; prompts the user (or auto-approves) to resume.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # noqa: E402  (must run before importing hireguard.*)

from langgraph.types import Command  # noqa: E402

from hireguard.db import get_checkpointer, save_audit_memo  # noqa: E402
from hireguard.graph import build_graph  # noqa: E402
from hireguard.state import HumanApproval, PipelineState, load_packet  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("run_demo")


def _resolve_packet_path(sample: str | None, packet: str | None) -> Path:
    if packet:
        return Path(packet)
    if sample:
        return Path("hireguard/samples") / f"{sample}.json"
    raise SystemExit("Pass --sample <name> or --packet <path>")


def _print_event(event: dict) -> None:
    for node_name, payload in event.items():
        if node_name == "__interrupt__":
            continue
        keys = list(payload.keys()) if isinstance(payload, dict) else "?"
        log.info("✔ node %-14s emitted keys=%s", node_name, keys)


async def _resolve_approval(args, payload: dict) -> dict:
    if args.auto_approve:
        log.info("[auto-approve] %s", args.decision)
        return {"decision": args.decision, "reviewer_note": "auto-approved by run_demo"}
    print("\n" + "=" * 70)
    print("  HUMAN-IN-THE-LOOP — REVIEW REQUIRED")
    print("=" * 70)
    print(f"Memo ID:   {payload.get('memo_id')}")
    print(f"Summary:   {payload.get('executive_summary')}")
    counts = payload.get("counts", {})
    print(
        f"Findings:  {counts.get('critical', 0)} critical | "
        f"{counts.get('high', 0)} high | "
        f"{counts.get('medium', 0)} medium | "
        f"{counts.get('low', 0)} low"
    )
    for i, f in enumerate(payload.get("findings_preview", []), 1):
        print(f"  {i}. [{f['severity'].upper():8}] {f['rule_id']:30} "
              f"score={f['exposure_score']}")
        print(f"     evidence: {f['evidence'][:100]}")
    print("=" * 70)
    raw = input("Decision [approve/reject/send_back]: ").strip().lower()
    if raw not in ("approve", "reject", "send_back"):
        raw = "reject"
    note = input("Reviewer note (optional): ").strip()
    return {"decision": raw, "reviewer_note": note}


async def main(args) -> int:
    packet_path = _resolve_packet_path(args.sample, args.packet)
    if not packet_path.exists():
        log.error("Packet not found: %s", packet_path)
        return 1
    packet = load_packet(str(packet_path))
    log.info("Loaded packet: %s (%s) @ %s",
             packet.packet_id, packet.company, packet.primary_work_location)

    async with get_checkpointer() as saver:
        graph = build_graph(checkpointer=saver)
        thread_id = f"demo-{packet.packet_id}"
        # Tags + metadata land in LangSmith — every run is filterable by
        # packet_id, jurisdiction, surface (cli/streamlit), member, etc.
        config = {
            "configurable": {"thread_id": thread_id},
            "tags": [
                "hireguard",
                "surface:cli",
                f"packet:{packet.packet_id}",
                f"jurisdiction:{packet.primary_work_location}",
                "mode:auto" if args.auto_approve else "mode:interactive",
            ],
            "metadata": {
                "thread_id": thread_id,
                "packet_id": packet.packet_id,
                "company": packet.company,
                "company_size": packet.company_size,
                "jurisdiction": packet.primary_work_location,
                "surface": "cli",
            },
        }
        init_state = PipelineState(packet=packet)

        log.info("─── Running pipeline (thread=%s) ───", thread_id)
        interrupt_payload = None
        async for event in graph.astream(init_state, config=config, stream_mode="updates"):
            _print_event(event)
            if "__interrupt__" in event:
                interrupt_payload = event["__interrupt__"][0].value
                break

        # If the graph paused at the HITL interrupt, resolve it and resume.
        while interrupt_payload is not None:
            approval_dict = await _resolve_approval(args, interrupt_payload)
            HumanApproval.model_validate(approval_dict)  # validate before sending

            interrupt_payload = None
            async for event in graph.astream(
                Command(resume=approval_dict),
                config=config,
                stream_mode="updates",
            ):
                _print_event(event)
                if "__interrupt__" in event:
                    interrupt_payload = event["__interrupt__"][0].value
                    break

        # Pull final state for the persist step.
        final = await graph.aget_state(config)
        approval = final.values.get("human_approval")
        memo = final.values.get("audit_memo")
        if approval and approval.decision == "approve" and memo:
            log.info("Persisting approved memo to Supabase…")
            await save_audit_memo(
                run_id=thread_id,
                packet_json=packet.model_dump_json(),
                memo_json=memo.model_dump_json(),
            )
            log.info("✅ Memo persisted.")
        elif approval:
            log.info("Decision=%s — not persisting.", approval.decision)
        else:
            log.warning("No approval captured — graph terminated without HITL resolution.")

        print("\n" + "─" * 70)
        if memo:
            print("FINAL MEMO")
            print("─" * 70)
            print(json.dumps(memo.model_dump(), indent=2, default=str))
        if final.values.get("errors"):
            print("\nWARNINGS / NOTES")
            for e in final.values["errors"]:
                print(f"  • {e}")
        return 0


def _parse_args():
    p = argparse.ArgumentParser(description="HireGuard v2 demo runner")
    p.add_argument("--sample", help="Sample packet name (e.g. acme_se_role)")
    p.add_argument("--packet", help="Path to a custom HiringPacket JSON file")
    p.add_argument("--auto-approve", action="store_true",
                   help="Skip interactive prompt; use --decision")
    p.add_argument("--decision", default="approve",
                   choices=["approve", "reject", "send_back"])
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
