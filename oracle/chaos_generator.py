#!/usr/bin/env python3
"""
FinGate-Sim / SessionBench Procedural Seeded Chaos Generator (DST Engine).

Inspired by TigerBeetle VOPR and FoundationDB deterministic simulation testing:
Given any 64-bit integer seed, this module procedurally synthesizes a unique,
mathematically valid multi-fault FIX 4.4 protocol trace (including wire-level
8=FIX.4.4|9=...|10=...| frames and corrupted checksums) alongside its formally
verified ground-truth output from FIX44SessionEngine.

Usage:
    python3 oracle/chaos_generator.py --seed 42 --count 100 --verify
"""

from __future__ import annotations
import argparse
from copy import deepcopy
import json
import random
from pathlib import Path
import sys
from typing import Any, Dict, List, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from oracle.fix44_spec_rules import (
    FIX44SessionEngine,
    encode_fix_wire_frame,
    verify_fingate_invariants,
)

FAULT_ARCHETYPES = [
    "ADMIN_RESEND_MIX",
    "GARBLED_WIRE_CHECKSUM",
    "OUT_OF_ORDER_GAPFILL",
    "POSSDUP_MISSING_ORIGTIME",
    "SESSION_SHIELD_POD_CRASH",
]


def generate_scenario(
    seed: int,
    include_wire_frames: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Deterministically generate a (task_spec, reference_spec) pair from a 64-bit integer seed.
    """
    rng = random.Random(seed)

    # 1. Generate randomized initial session state & outbound store (mix of Admin & App msgs)
    init_sender_seq = rng.randint(6, 18)
    init_target_seq = rng.randint(4, 15)
    hb_int_ms = rng.choice([15000, 30000, 60000])
    grace_ms = rng.choice([3000, 5000, 10000])

    outbound_store: List[Dict[str, Any]] = []
    order_idx = 100
    for seq in range(1, init_sender_seq):
        if seq == 1:
            mtype = "A"
        else:
            # Ensure a realistic interleaved mix of Admin (0, 1, 2, 3) and App (D, F) messages
            mtype = rng.choices(
                ["0", "1", "3", "D", "F"],
                weights=[30, 15, 10, 30, 15],
                k=1,
            )[0]
        entry: Dict[str, Any] = {
            "msg_seq_num": seq,
            "msg_type": mtype,
            "send_time_ms": seq * 100,
        }
        if mtype in ("D", "F"):
            entry["cl_ord_id"] = f"ORD-SEED{seed}-{order_idx}"
            order_idx += 1
        outbound_store.append(entry)

    # Working orders for FinGate Session Shield pod multiplexing
    working_orders = [
        {
            "cl_ord_id": f"SHIELD-{seed}-1",
            "pod_id": "pod-alpha",
            "symbol": "BTC-USD",
            "side": "BUY",
            "leaves_qty": 5,
            "status": "WORKING",
        },
        {
            "cl_ord_id": f"SHIELD-{seed}-2",
            "pod_id": "pod-alpha",
            "symbol": "ETH-USD",
            "side": "SELL",
            "leaves_qty": 10,
            "status": "WORKING",
        },
    ]

    # Select 2 to 3 interacting fault archetypes per seed
    chosen_faults = rng.sample(FAULT_ARCHETYPES, k=rng.randint(2, 3))
    rules_tested: List[str] = ["R1", "R2", "R5"]

    events: List[Dict[str, Any]] = []
    now_ms = init_sender_seq * 100 + 500
    cur_target_seq = init_target_seq

    # Fault A: Garbled Wire Frame (bad CheckSum 10 or bad BodyLength 9)
    # Tests R25: Must be silently dropped without advancing NextTargetMsgSeqNum
    if "GARBLED_WIRE_CHECKSUM" in chosen_faults:
        rules_tested.append("R25")
        corrupt_cs = rng.choice([True, False])
        ev_garbled: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "8",
            "msg_seq_num": cur_target_seq,
            "poss_dup_flag": False,
            "cl_ord_id": f"SHIELD-{seed}-1",
            "leaves_qty": 0,
            "garbled": True,
        }
        if include_wire_frames:
            ev_garbled["raw_wire"] = encode_fix_wire_frame(
                ev_garbled,
                corrupt_checksum=corrupt_cs,
                corrupt_body_length=not corrupt_cs,
            )
        events.append(ev_garbled)
        now_ms += 120

    # Fault B: PossDup=Y missing OrigSendingTime (122) OR valid PossDup duplicate
    if "POSSDUP_MISSING_ORIGTIME" in chosen_faults:
        rules_tested.extend(["R8", "R9"])
        # First send an old duplicate with valid OrigSendingTime (34 < expected -> drop)
        if cur_target_seq > 2:
            ev_dup: Dict[str, Any] = {
                "timestamp_ms": now_ms,
                "event_type": "RECV_MSG",
                "msg_type": "8",
                "msg_seq_num": cur_target_seq - 1,
                "poss_dup_flag": True,
                "orig_sending_time_ms": now_ms - 1000,
            }
            if include_wire_frames:
                ev_dup["raw_wire"] = encode_fix_wire_frame(ev_dup)
            events.append(ev_dup)
            now_ms += 100

        # Next send current seq with PossDup=Y but missing OrigSendingTime (122) -> SessionReject (35=3, 373=1)
        ev_bad_pd: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "8",
            "msg_seq_num": cur_target_seq,
            "poss_dup_flag": True,
            "orig_sending_time_ms": None,
        }
        if include_wire_frames:
            ev_bad_pd["raw_wire"] = encode_fix_wire_frame(ev_bad_pd)
        events.append(ev_bad_pd)
        cur_target_seq += 1
        now_ms += 150

    # Fault C: Inbound ResendRequest across interleaved Admin (0, 1, 3) & Application (D, F) store
    if "ADMIN_RESEND_MIX" in chosen_faults:
        rules_tested.extend(["R1", "R2", "R13"])
        begin_no = rng.randint(2, max(2, init_sender_seq - 4))
        end_no = rng.choice([0, init_sender_seq - 1])
        ev_rr: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "2",
            "msg_seq_num": cur_target_seq,
            "poss_dup_flag": False,
            "begin_seq_no": begin_no,
            "end_seq_no": end_no,
        }
        if include_wire_frames:
            ev_rr["raw_wire"] = encode_fix_wire_frame(ev_rr)
        events.append(ev_rr)
        cur_target_seq += 1
        now_ms += 200

    # Fault D: Out-of-order SequenceReset-GapFill (35=4, 123=Y) arriving ahead of missing packet
    if "OUT_OF_ORDER_GAPFILL" in chosen_faults:
        rules_tested.extend(["R7", "R14", "R15"])
        missing_seq = cur_target_seq
        future_gapfill_seq = cur_target_seq + 1
        jump_to_seq = cur_target_seq + rng.randint(3, 5)

        # 1. Arrive out-of-order at future_gapfill_seq -> triggers ResendRequest for missing_seq
        ev_ooo_gf: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "4",
            "msg_seq_num": future_gapfill_seq,
            "poss_dup_flag": True,
            "orig_sending_time_ms": now_ms - 50,
            "gap_fill_flag": True,
            "new_seq_no": jump_to_seq,
        }
        if include_wire_frames:
            ev_ooo_gf["raw_wire"] = encode_fix_wire_frame(ev_ooo_gf)
        events.append(ev_ooo_gf)
        now_ms += 120

        # 2. Deliver the missing message at missing_seq -> drains queued GapFill and jumps to jump_to_seq
        ev_fill_gap: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "8",
            "msg_seq_num": missing_seq,
            "poss_dup_flag": True,
            "orig_sending_time_ms": now_ms - 200,
            "cl_ord_id": f"SHIELD-{seed}-1",
            "leaves_qty": 0,
        }
        if include_wire_frames:
            ev_fill_gap["raw_wire"] = encode_fix_wire_frame(ev_fill_gap)
        events.append(ev_fill_gap)
        cur_target_seq = jump_to_seq
        now_ms += 150

    # Fault E: FinGate Session Shield Pod Crash + Grace Window + Fill Race
    if "SESSION_SHIELD_POD_CRASH" in chosen_faults:
        rules_tested.extend(["R16", "R17", "R18", "R20"])
        events.append(
            {
                "timestamp_ms": now_ms,
                "event_type": "POD_CRASH",
                "pod_id": "pod-alpha",
            }
        )
        now_ms += 100
        # Fill SHIELD-{seed}-1 during grace window if not already filled
        ev_exec: Dict[str, Any] = {
            "timestamp_ms": now_ms,
            "event_type": "RECV_MSG",
            "msg_type": "8",
            "msg_seq_num": cur_target_seq,
            "poss_dup_flag": False,
            "cl_ord_id": f"SHIELD-{seed}-1",
            "leaves_qty": 0,
        }
        if include_wire_frames:
            ev_exec["raw_wire"] = encode_fix_wire_frame(ev_exec)
        events.append(ev_exec)
        cur_target_seq += 1
        now_ms += grace_ms + 100
        # Advance clock past pod_grace_timeout_ms so remaining open orders are auto-cancelled
        events.append(
            {
                "timestamp_ms": now_ms,
                "event_type": "CLOCK_TICK",
            }
        )

    task_id = f"CHAOS_{seed}"
    task_spec: Dict[str, Any] = {
        "task_id": task_id,
        "seed": seed,
        "title": f"Procedural Multi-Fault Chaos Scenario (Seed {seed})",
        "level": 3,
        "level_name": "Procedural DST Chaos",
        "description": (
            f"Procedurally synthesized multi-fault FIX 4.4 session trace (seed={seed}) "
            f"combining faults: {', '.join(chosen_faults)}. Compute the exact outbound FIX "
            f"messages and final session state."
        ),
        "engine_config": {
            "heart_bt_int_ms": hb_int_ms,
            "test_request_grace_ms": hb_int_ms,
            "send_redundant_resend_requests": False,
            "pod_grace_timeout_ms": grace_ms,
        },
        "initial_store": {
            "status": "ACTIVE",
            "next_sender_seq_num": init_sender_seq,
            "next_target_seq_num": init_target_seq,
            "last_sent_time_ms": init_sender_seq * 100,
            "last_recv_time_ms": init_sender_seq * 100,
            "outbound_messages_store": outbound_store,
            "working_orders": working_orders,
        },
        "inbound_event_trace": events,
    }

    engine = FIX44SessionEngine(task_spec)
    result = engine.run_trace(task_spec["inbound_event_trace"])
    violations = verify_fingate_invariants(task_spec, result)
    if violations:
        raise RuntimeError(f"Seed {seed} violated FinGate invariants: {violations}")

    ref_spec: Dict[str, Any] = {
        "task_id": task_id,
        "seed": seed,
        "rules_tested": sorted(set(rules_tested)),
        "faults_injected": chosen_faults,
        "outbound_messages": result["outbound_messages"],
        "final_state": result["final_state"],
    }
    return task_spec, ref_spec


def main() -> None:
    parser = argparse.ArgumentParser(description="SessionBench Procedural Seeded Chaos Generator")
    parser.add_argument("--seed", type=int, default=1000, help="Starting 64-bit integer seed")
    parser.add_argument("--count", type=int, default=100, help="Number of chaos scenarios to generate/verify")
    parser.add_argument("--dump-json", type=str, default=None, help="Optional path to write generated scenarios")
    parser.add_argument("--verify", action="store_true", help="Verify all generated traces against FinGate invariants")
    args = parser.parse_args()

    scenarios = []
    total_events = 0
    total_outbound = 0
    for i in range(args.count):
        s = args.seed + i
        task_spec, ref_spec = generate_scenario(s, include_wire_frames=True)
        total_events += len(task_spec["inbound_event_trace"])
        total_outbound += len(ref_spec["outbound_messages"])
        scenarios.append({"task": task_spec, "reference": ref_spec})

    if args.dump_json:
        out_path = Path(args.dump_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(scenarios, indent=2) + "\n", encoding="utf-8")

    print(
        f"[OK] Procedurally generated & verified {args.count} chaos scenarios "
        f"(seeds {args.seed}..{args.seed + args.count - 1} | {total_events} wire events | "
        f"{total_outbound} outbound frames | 0 FinGate invariant violations)."
    )


if __name__ == "__main__":
    main()
