"""Minimal Streamlit shell for HireGuard v2.

Three tabs:
  - Run Audit         → pick a sample packet, run the graph until HITL pause
  - Pending Approval  → render the paused memo, Approve / Reject / Send back
  - History           → list approved audit memos from Supabase

Run with:  streamlit run hireguard/ui/streamlit_app.py
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from langgraph.types import Command  # noqa: E402

from hireguard.db import get_checkpointer, save_audit_memo  # noqa: E402
from hireguard.graph import build_graph  # noqa: E402
from hireguard.state import HiringPacket, PipelineState  # noqa: E402

st.set_page_config(page_title="HireGuard v2", layout="wide", page_icon="🛡")
st.title("🛡 HireGuard v2")
st.caption(
    "Multi-agent AI auditor for U.S. hiring compliance — "
    "LangGraph · Claude · Supabase · LangSmith"
)

SAMPLES_DIR = Path("hireguard/samples")


def _run_async(coro):
    """Streamlit doesn't play nicely with asyncio.run inside callbacks; use a fresh loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _trace_config(packet: HiringPacket, thread_id: str, phase: str) -> dict:
    """Build the LangGraph run config with LangSmith tags + metadata."""
    return {
        "configurable": {"thread_id": thread_id},
        "tags": [
            "hireguard",
            "surface:streamlit",
            f"phase:{phase}",
            f"packet:{packet.packet_id}",
            f"jurisdiction:{packet.primary_work_location}",
        ],
        "metadata": {
            "thread_id": thread_id,
            "packet_id": packet.packet_id,
            "company": packet.company,
            "company_size": packet.company_size,
            "jurisdiction": packet.primary_work_location,
            "surface": "streamlit",
            "phase": phase,
        },
    }


async def _run_until_pause(packet: HiringPacket, thread_id: str):
    async with get_checkpointer() as saver:
        graph = build_graph(checkpointer=saver)
        config = _trace_config(packet, thread_id, phase="initial")
        events: list[dict] = []
        interrupt_payload = None
        async for ev in graph.astream(
            PipelineState(packet=packet),
            config=config,
            stream_mode="updates",
        ):
            events.append(ev)
            if "__interrupt__" in ev:
                interrupt_payload = ev["__interrupt__"][0].value
                break
        snap = await graph.aget_state(config)
        return events, interrupt_payload, snap.values


async def _resume_with(approval: dict, thread_id: str):
    async with get_checkpointer() as saver:
        graph = build_graph(checkpointer=saver)
        # Resume events correlate to the same trace via thread_id; tag this leg
        # so the LangSmith UI can show approve/reject/send-back as distinct spans.
        config = {
            "configurable": {"thread_id": thread_id},
            "tags": ["hireguard", "surface:streamlit",
                     "phase:resume", f"decision:{approval.get('decision','?')}"],
            "metadata": {"thread_id": thread_id, "phase": "resume",
                         "decision": approval.get("decision")},
        }
        events: list[dict] = []
        async for ev in graph.astream(
            Command(resume=approval),
            config=config,
            stream_mode="updates",
        ):
            events.append(ev)
        snap = await graph.aget_state(config)
        return events, snap.values


tab_run, tab_review, tab_history = st.tabs(
    ["🔍 Run Audit", "⏸ Pending Approval", "📜 History"]
)


# ─── Run Audit ──────────────────────────────────────────────────────────────
with tab_run:
    st.subheader("Run a new audit")
    sample_files = sorted(p.stem for p in SAMPLES_DIR.glob("*.json"))
    choice = st.selectbox("Sample packet", sample_files + ["(custom JSON)"])
    if choice == "(custom JSON)":
        raw_json = st.text_area("Paste a HiringPacket JSON", height=300)
        if not raw_json.strip():
            st.info("Paste a packet JSON to enable Run.")
            packet_json = None
        else:
            packet_json = raw_json
    else:
        packet_json = (SAMPLES_DIR / f"{choice}.json").read_text()
        with st.expander("Preview packet"):
            st.code(packet_json, language="json")

    if st.button("▶ Run audit", type="primary", disabled=packet_json is None):
        packet = HiringPacket.model_validate_json(packet_json)
        thread_id = f"ui-{packet.packet_id}"
        with st.status("Running multi-agent pipeline…", expanded=True) as status:
            events, interrupt_payload, snap = _run_async(
                _run_until_pause(packet, thread_id)
            )
            for ev in events:
                for node, _payload in ev.items():
                    if node != "__interrupt__":
                        st.write(f"✔ **{node}** complete")
            if interrupt_payload is None:
                status.update(label="Pipeline finished without HITL pause", state="complete")
            else:
                status.update(label="Paused at human-review gate", state="complete")
        if interrupt_payload is not None:
            st.session_state["pending"] = {
                "thread_id": thread_id,
                "payload": interrupt_payload,
                "snap": snap,
            }
            st.success("Move to the **Pending Approval** tab to review.")
        else:
            st.info("Graph terminated. Check the History tab.")


# ─── Pending Approval ───────────────────────────────────────────────────────
with tab_review:
    st.subheader("Human-in-the-loop gate")
    if "pending" not in st.session_state:
        st.info("No audit pending. Start one in the **Run Audit** tab.")
    else:
        p = st.session_state["pending"]
        payload = p["payload"]
        snap = p["snap"]
        memo = snap.get("audit_memo")

        col1, col2 = st.columns([2, 1])
        with col1:
            st.markdown(f"**Memo ID:** `{payload.get('memo_id')}`")
            st.markdown("**Executive summary:**")
            st.write(payload.get("executive_summary", ""))
        with col2:
            counts = payload.get("counts", {})
            st.metric("Critical", counts.get("critical", 0))
            st.metric("High", counts.get("high", 0))
            st.metric("Medium", counts.get("medium", 0))
            st.metric("Low", counts.get("low", 0))

        st.markdown("### Findings")
        sev_color = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        for i, f in enumerate(payload.get("findings_preview", []), 1):
            tag = sev_color.get(f["severity"], "⚪")
            with st.expander(
                f"{tag} {f['rule_id']} — score {f['exposure_score']} "
                f"({f['severity'].upper()})"
            ):
                st.markdown(f"**Evidence:** _{f['evidence']}_")

        st.markdown("### Decision")
        note = st.text_area("Reviewer note (optional)", value="")
        c1, c2, c3 = st.columns(3)
        if c1.button("✅ Approve", type="primary"):
            approval = {"decision": "approve", "reviewer_note": note}
            _, final_snap = _run_async(_resume_with(approval, p["thread_id"]))
            if memo:
                _run_async(
                    save_audit_memo(
                        run_id=p["thread_id"],
                        packet_json=PipelineState.model_validate(snap).packet.model_dump_json(),
                        memo_json=json.dumps(memo if isinstance(memo, dict) else memo.model_dump(), default=str),
                    )
                )
            st.success("Approved + persisted.")
            del st.session_state["pending"]
            st.rerun()
        if c2.button("↩ Send back"):
            approval = {"decision": "send_back", "reviewer_note": note}
            _, final_snap = _run_async(_resume_with(approval, p["thread_id"]))
            st.warning("Sent back to Policy for re-check.")
            # leave pending so user can review again after re-loop completes (advanced)
        if c3.button("❌ Reject"):
            approval = {"decision": "reject", "reviewer_note": note}
            _run_async(_resume_with(approval, p["thread_id"]))
            st.error("Rejected.")
            del st.session_state["pending"]
            st.rerun()


# ─── History ────────────────────────────────────────────────────────────────
with tab_history:
    st.subheader("Past approved audits")
    try:
        import os
        import psycopg

        url = os.environ.get("SUPABASE_DB_URL", "")
        if not url:
            st.warning("SUPABASE_DB_URL not set.")
        else:
            with psycopg.connect(url, prepare_threshold=None) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT run_id, memo->>'executive_summary', "
                        "memo->>'critical_count', approved_at "
                        "FROM audit_memos ORDER BY approved_at DESC LIMIT 50"
                    )
                    rows = cur.fetchall()
            if not rows:
                st.info("No audits persisted yet.")
            else:
                for run_id, summary, crit, ts in rows:
                    with st.expander(f"{ts} — `{run_id}` (crit={crit})"):
                        st.write(summary)
    except Exception as e:
        st.error(f"Could not query history: {type(e).__name__}: {e}")
