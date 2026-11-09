#!/usr/bin/env python3
"""
SessionBench Reference Oracle & Task Generator.

Generates the 10 parameterized tasks across the 3-level difficulty ladder (k=2..5 interacting rules)
and executes them through the formal FIX 4.4 + FinGate Session Shield state machine oracle
to produce verified ground-truth reference outputs in `reference/ref_01.json` .. `ref_10.json`.

Design Invariant (No Prompt Spoilers - Page 12 of Participant Guide):
- Task files (`tasks/task_XX_*.json`) contain ONLY neutral scenario metadata, `engine_config`,
  `initial_store`, and `inbound_event_trace`. They never leak expected sequence numbers,
  expected message types, or rule hints.
- Reference files (`reference/ref_XX.json`) store `outbound_messages`, `final_state`,
  `rules_tested`, and `expert_rationale`.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from oracle.fix44_spec_rules import FIX44SessionEngine, verify_fingate_invariants

BASE_DIR = Path(__file__).resolve().parent.parent
TASKS_DIR = BASE_DIR / "tasks"
REF_DIR = BASE_DIR / "reference"


def build_tasks() -> List[Dict[str, Any]]:
    """Construct the 10 canonical SessionBench tasks across Levels 1, 2, and 3."""
    return [
        # ---------------------------------------------------------------------
        # TASK 01 (Level 1, k=2): Mixed Resend & Admin Gap-Fill Coalescing
        # ---------------------------------------------------------------------
        {
            "task_id": "T01",
            "filename": "task_01_admin_gapfill.json",
            "ref_filename": "ref_01.json",
            "level": 1,
            "interacting_rules_count": 2,
            "rules_tested": [
                "R1: Admin vs. Application Message Classification during Resend",
                "R2: Contiguous Admin Message Coalescing into SequenceReset-GapFill (35=4, 123=Y, 36=last_admin+1)",
            ],
            "expert_rationale": (
                "Stored messages 4..9 contain admin messages at 4 (35=0), 6 (35=1), and 7 (35=0), and application "
                "orders (35=D) at 5, 8, and 9. Admin messages must never be resent verbatim; contiguous admin messages "
                "6 and 7 must coalesce into a single SequenceReset-GapFill (35=4, 123=Y, 34=6, 36=8)."
            ),
            "title": "Outbound Replay Across Mixed Session & Order History",
            "description": (
                "An active FIX 4.4 session with NextSenderMsgSeqNum=10 and NextTargetMsgSeqNum=3 holds outbound "
                "history for sequence numbers 1 through 9. At t=2000ms, an inbound ResendRequest (35=2) arrives "
                "requesting sequence range 4 through 9."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 10,
                "next_target_seq_num": 3,
                "last_sent_time_ms": 1800,
                "last_recv_time_ms": 1500,
                "outbound_messages_store": [
                    {"msg_seq_num": 1, "msg_type": "A", "send_time_ms": 100},
                    {"msg_seq_num": 2, "msg_type": "0", "send_time_ms": 500},
                    {"msg_seq_num": 3, "msg_type": "D", "cl_ord_id": "ORD-1", "send_time_ms": 800},
                    {"msg_seq_num": 4, "msg_type": "0", "send_time_ms": 1000},
                    {"msg_seq_num": 5, "msg_type": "D", "cl_ord_id": "ORD-2", "send_time_ms": 1200},
                    {"msg_seq_num": 6, "msg_type": "1", "send_time_ms": 1400},
                    {"msg_seq_num": 7, "msg_type": "0", "send_time_ms": 1600},
                    {"msg_seq_num": 8, "msg_type": "D", "cl_ord_id": "ORD-3", "send_time_ms": 1700},
                    {"msg_seq_num": 9, "msg_type": "D", "cl_ord_id": "ORD-4", "send_time_ms": 1800},
                ],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2000,
                    "msg_type": "2",
                    "msg_seq_num": 3,
                    "begin_seq_no": 4,
                    "end_seq_no": 9,
                    "send_time_ms": 2000,
                }
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 02 (Level 1, k=2): Timer Arithmetic — TestRequest & Disconnect
        # ---------------------------------------------------------------------
        {
            "task_id": "T02",
            "filename": "task_02_heartbeat_timer.json",
            "ref_filename": "ref_02.json",
            "level": 1,
            "interacting_rules_count": 2,
            "rules_tested": [
                "R3: Outbound Heartbeat (35=0) emission on idle send timer >= HeartBtInt",
                "R4: Inbound silence >= HeartBtInt triggers TestRequest (35=1); silence >= HeartBtInt + Grace triggers TCP disconnect",
            ],
            "expert_rationale": (
                "Outbound Heartbeats depend on time since last sent message (t=30000ms, t=60000ms), whereas TestRequest "
                "depends on time since last received message (t=41000ms + 30000ms = 71000ms), followed by disconnect at 101000ms."
            ),
            "title": "Asynchronous Inbound Traffic & Session Liveness Timers",
            "description": (
                "An active FIX 4.4 session is configured with heart_bt_int_ms=30000 and test_request_grace_ms=30000. "
                "Inbound messages arrive at t=12000ms and t=41000ms interspersed with periodic clock ticks up to t=101000ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "test_request_grace_ms": 30000,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 5,
                "next_target_seq_num": 8,
                "last_sent_time_ms": 0,
                "last_recv_time_ms": 0,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 12000,
                    "msg_type": "D",
                    "msg_seq_num": 8,
                    "send_time_ms": 12000,
                },
                {"event_type": "CLOCK_TICK", "timestamp_ms": 30000},
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 41000,
                    "msg_type": "0",
                    "msg_seq_num": 9,
                    "send_time_ms": 41000,
                },
                {"event_type": "CLOCK_TICK", "timestamp_ms": 60000},
                {"event_type": "CLOCK_TICK", "timestamp_ms": 71000},
                {"event_type": "CLOCK_TICK", "timestamp_ms": 101000},
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 03 (Level 2, k=3): Unclean Crash & Warm Reconnect Deficit
        # ---------------------------------------------------------------------
        {
            "task_id": "T03",
            "filename": "task_03_crash_reconnect.json",
            "ref_filename": "ref_03.json",
            "level": 2,
            "interacting_rules_count": 3,
            "rules_tested": [
                "R5: Persistent sequence number continuity across crashes",
                "R6: Logon (35=A) acknowledgement must precede ResendRequest (35=2) on reconnect gap",
                "R7: Inbound sequence gap (34 > NextTargetMsgSeqNum) triggers ResendRequest (7=expected, 16=0) and RESENDING state",
            ],
            "expert_rationale": (
                "Upon receiving Logon (35=A, 34=25) when expecting 22, the engine must first send Logon (35=A, 34=15), "
                "then emit ResendRequest (35=2, 34=16, BeginSeqNo=22, EndSeqNo=0), preserve NextTargetMsgSeqNum=22, and enter RESENDING."
            ),
            "title": "Warm Session Reconnection After Process Restart",
            "description": (
                "A session resumes in DISCONNECTED status with persisted sequence counters NextSenderMsgSeqNum=15 "
                "and NextTargetMsgSeqNum=22. At t=50000ms, an inbound Logon (35=A) arrives from the counterparty with MsgSeqNum=25."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "DISCONNECTED",
                "next_sender_seq_num": 15,
                "next_target_seq_num": 22,
                "last_sent_time_ms": 40000,
                "last_recv_time_ms": 40000,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 50000,
                    "msg_type": "A",
                    "msg_seq_num": 25,
                    "send_time_ms": 50000,
                }
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 04 (Level 2, k=3): PossDupFlag vs OrigSendingTime & Seq Too Low
        # ---------------------------------------------------------------------
        {
            "task_id": "T04",
            "filename": "task_04_possdup_clockskew.json",
            "ref_filename": "ref_04.json",
            "level": 2,
            "interacting_rules_count": 3,
            "rules_tested": [
                "R8: PossDupFlag=Y with MsgSeqNum < NextTargetMsgSeqNum and valid OrigSendingTime is safely ignored",
                "R9: PossDupFlag=Y missing required OrigSendingTime (122) triggers Session Reject (35=3, 373=1) and advances TargetSeq",
                "R10: MsgSeqNum < NextTargetMsgSeqNum with PossDupFlag=N triggers immediate fatal Logout (35=5) without advancing TargetSeq",
            ],
            "expert_rationale": (
                "Packet 1 (34=18 < 20, PossDup=Y, OrigSendingTime=500) is safely dropped. Packet 2 (34=20, PossDup=Y, "
                "OrigSendingTime=None) triggers Reject (35=3, RefSeqNum=20, SessionRejectReason=1) and advances TargetSeq to 21. "
                "Packet 3 (34=19 < 21, PossDup=N) triggers fatal Logout (35=5) and disconnects without advancing TargetSeq."
            ),
            "title": "Inbound Sequence Anomalies & Header Validation",
            "description": (
                "An active session at NextSenderMsgSeqNum=10 and NextTargetMsgSeqNum=20 receives three consecutive "
                "inbound NewOrderSingle (35=D) messages at t=1000ms, t=2000ms, and t=3000ms with varying sequence numbers and header flags."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 10,
                "next_target_seq_num": 20,
                "last_sent_time_ms": 0,
                "last_recv_time_ms": 0,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 1000,
                    "msg_type": "D",
                    "msg_seq_num": 18,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 500,
                    "send_time_ms": 1000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2000,
                    "msg_type": "D",
                    "msg_seq_num": 20,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": None,
                    "send_time_ms": 2000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 3000,
                    "msg_type": "D",
                    "msg_seq_num": 19,
                    "poss_dup_flag": False,
                    "send_time_ms": 3000,
                },
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 05 (Level 2, k=3): SeqReset-GapFill (123=Y) vs SeqReset-Reset (123=N)
        # ---------------------------------------------------------------------
        {
            "task_id": "T05",
            "filename": "task_05_seqreset_modes.json",
            "ref_filename": "ref_05.json",
            "level": 2,
            "interacting_rules_count": 3,
            "rules_tested": [
                "R11: SequenceReset-GapFill (123=Y) attempting to lower NewSeqNo (36 < expected) triggers Session Reject (35=3, 373=5)",
                "R12: SequenceReset-Reset (123=N) ignores its own MsgSeqNum (34) and unconditionally sets NextTargetMsgSeqNum = NewSeqNo",
                "R3: Subsequent TestRequest (35=1) at the newly reset sequence number triggers Heartbeat (35=0)",
            ],
            "expert_rationale": (
                "Msg 1 (35=4, 123=Y, 34=15, NewSeqNo=12 < 15) is rejected with 35=3, SessionRejectReason=5 and advances TargetSeq to 16. "
                "Msg 2 (35=4, 123=N, 34=1, NewSeqNo=25) unconditionally sets TargetSeq=25. Msg 3 (35=1, 34=25) is echoed with 35=0."
            ),
            "title": "SequenceReset Mode Transitions & Subsequent Liveness Check",
            "description": (
                "An active session at NextSenderMsgSeqNum=8 and NextTargetMsgSeqNum=15 receives two SequenceReset (35=4) "
                "messages at t=1000ms and t=2000ms, followed by an inbound TestRequest (35=1) at t=3000ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 8,
                "next_target_seq_num": 15,
                "last_sent_time_ms": 0,
                "last_recv_time_ms": 0,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 1000,
                    "msg_type": "4",
                    "msg_seq_num": 15,
                    "gap_fill_flag": True,
                    "new_seq_no": 12,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 900,
                    "send_time_ms": 1000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2000,
                    "msg_type": "4",
                    "msg_seq_num": 1,
                    "gap_fill_flag": False,
                    "new_seq_no": 25,
                    "poss_dup_flag": False,
                    "send_time_ms": 2000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 3000,
                    "msg_type": "1",
                    "msg_seq_num": 25,
                    "test_req_id": "PING-1",
                    "send_time_ms": 3000,
                },
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 06 (Level 2, k=3): Open-Ended Resend (16=0) + Admin 35=3 Reject Suppression
        # ---------------------------------------------------------------------
        {
            "task_id": "T06",
            "filename": "task_06_chunked_resend_infinity.json",
            "ref_filename": "ref_06.json",
            "level": 2,
            "interacting_rules_count": 3,
            "rules_tested": [
                "R1: Session Reject (35=3) is an Administrative Message and must NEVER be resent verbatim",
                "R2: Contiguous Admin Messages (35=3 at seq 11 and 35=0 at seq 12) coalesce into a single GapFill (34=11, 36=13)",
                "R13: Open-Ended ResendRequest (EndSeqNo 16=0) resolves upper bound to NextSenderMsgSeqNum - 1",
            ],
            "expert_rationale": (
                "EndSeqNo=0 replays 10..14. Stored msg 11 is a SessionReject (35=3), which is an admin message and must "
                "coalesce with msg 12 (35=0) into GapFill (34=11, 36=13), followed by order 13 and GapFill (34=14, 36=15)."
            ),
            "title": "Open-Ended Replay With Prior Session-Level Rejections",
            "description": (
                "An active session at NextSenderMsgSeqNum=15 and NextTargetMsgSeqNum=10 stores outbound messages 10..14 "
                "(comprising orders, a session reject, a heartbeat, and a test request). At t=4000ms, the counterparty "
                "sends an inbound ResendRequest (35=2) with BeginSeqNo=10 and EndSeqNo=0."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 15,
                "next_target_seq_num": 10,
                "last_sent_time_ms": 3500,
                "last_recv_time_ms": 3500,
                "outbound_messages_store": [
                    {"msg_seq_num": 10, "msg_type": "D", "cl_ord_id": "ORD-10", "send_time_ms": 1000},
                    {"msg_seq_num": 11, "msg_type": "3", "ref_seq_num": 5, "session_reject_reason": 1, "send_time_ms": 1500},
                    {"msg_seq_num": 12, "msg_type": "0", "send_time_ms": 2000},
                    {"msg_seq_num": 13, "msg_type": "D", "cl_ord_id": "ORD-13", "send_time_ms": 2500},
                    {"msg_seq_num": 14, "msg_type": "1", "send_time_ms": 3000},
                ],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 4000,
                    "msg_type": "2",
                    "msg_seq_num": 10,
                    "begin_seq_no": 10,
                    "end_seq_no": 0,
                    "send_time_ms": 4000,
                }
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 07 (Level 3, k=4): Dueling Bidirectional ResendRequest Collision
        # ---------------------------------------------------------------------
        {
            "task_id": "T07",
            "filename": "task_07_dueling_resend_collision.json",
            "ref_filename": "ref_07.json",
            "level": 3,
            "interacting_rules_count": 4,
            "rules_tested": [
                "R7: Inbound sequence gap (34=22 > expected 20) triggers ResendRequest (7=20, 16=0) and RESENDING state",
                "R14: SendRedundantResendRequests=False suppresses duplicate ResendRequest when 34=23 arrives during RESENDING",
                "R1: Counterparty ResendRequest (34=23) is serviced immediately during bidirectional gap to prevent deadlock",
                "R15: Inbound GapFill (34=20, 36=22) resolves gap and drains queued messages 22 and 23 to advance TargetSeq to 24",
            ],
            "expert_rationale": (
                "Msg 34=22 at t=1000ms triggers ResendRequest(20..0). Msg 34=23 (ResendRequest 10..11) at t=1500ms does not "
                "trigger a redundant ResendRequest, but is serviced immediately (GapFill 10->11, Order 11). At t=2000ms, "
                "GapFill (34=20, 36=22) arrives and drains queued 22 and 23 -> TargetSeq=24, status=ACTIVE."
            ),
            "title": "Concurrent Bidirectional Sequence Gaps & Recovery",
            "description": (
                "An active session with send_redundant_resend_requests=False, NextSenderMsgSeqNum=12, and "
                "NextTargetMsgSeqNum=20 receives an inbound order (34=22) at t=1000ms, an inbound ResendRequest "
                "(34=23, BeginSeqNo=10, EndSeqNo=11) at t=1500ms, and an inbound SequenceReset (34=20, NewSeqNo=22) at t=2000ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 12,
                "next_target_seq_num": 20,
                "last_sent_time_ms": 500,
                "last_recv_time_ms": 500,
                "outbound_messages_store": [
                    {"msg_seq_num": 10, "msg_type": "0", "send_time_ms": 200},
                    {"msg_seq_num": 11, "msg_type": "D", "cl_ord_id": "ORD-11", "send_time_ms": 400},
                ],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 1000,
                    "msg_type": "D",
                    "msg_seq_num": 22,
                    "send_time_ms": 1000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 1500,
                    "msg_type": "2",
                    "msg_seq_num": 23,
                    "begin_seq_no": 10,
                    "end_seq_no": 11,
                    "send_time_ms": 1500,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2000,
                    "msg_type": "4",
                    "msg_seq_num": 20,
                    "gap_fill_flag": True,
                    "new_seq_no": 22,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 1800,
                    "send_time_ms": 2000,
                },
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 08 (Level 3, k=4): Out-of-Order GapFill Arriving Ahead of Missing Gap
        # ---------------------------------------------------------------------
        {
            "task_id": "T08",
            "filename": "task_08_out_of_order_gapfill.json",
            "ref_filename": "ref_08.json",
            "level": 3,
            "interacting_rules_count": 4,
            "rules_tested": [
                "R11: SequenceReset-GapFill (123=Y) must obey sequence ordering and cannot jump over missing preceding messages",
                "R7: Out-of-order GapFill (34=12 > expected 10) triggers ResendRequest (7=10, 16=0) and is queued",
                "R14: Subsequent resent messages 10 and 11 advance NextTargetMsgSeqNum step-by-step to 12",
                "R15: Once NextTargetMsgSeqNum reaches 12, queued GapFill (34=12, 36=16) executes and jumps TargetSeq to 16",
            ],
            "expert_rationale": (
                "Because GapFillFlag=True (123=Y), the SequenceReset at 34=12 (> expected 10) cannot bypass missing "
                "messages 10 and 11. Engine emits ResendRequest(10..0), queues msg 12, processes resent 10 and 11, "
                "and then applies the queued 12->16 GapFill to reach NextTargetMsgSeqNum=16."
            ),
            "title": "Non-Contiguous SequenceReset Reception & Replay Stream",
            "description": (
                "An active session at NextSenderMsgSeqNum=5 and NextTargetMsgSeqNum=10 receives an inbound "
                "SequenceReset (35=4, gap_fill_flag=True, MsgSeqNum=12, NewSeqNo=16) at t=1000ms, followed by "
                "inbound NewOrderSingle messages with MsgSeqNum=10 at t=2000ms and MsgSeqNum=11 at t=2500ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 5,
                "next_target_seq_num": 10,
                "last_sent_time_ms": 500,
                "last_recv_time_ms": 500,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 1000,
                    "msg_type": "4",
                    "msg_seq_num": 12,
                    "gap_fill_flag": True,
                    "new_seq_no": 16,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 800,
                    "send_time_ms": 1000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2000,
                    "msg_type": "D",
                    "msg_seq_num": 10,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 600,
                    "send_time_ms": 2000,
                },
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 2500,
                    "msg_type": "D",
                    "msg_seq_num": 11,
                    "poss_dup_flag": True,
                    "orig_sending_time_ms": 700,
                    "send_time_ms": 2500,
                },
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 09 (Level 3, k=5): FinGate Session Shield & Synthetic Cancel-on-Disconnect
        # ---------------------------------------------------------------------
        {
            "task_id": "T09",
            "filename": "task_09_session_shield_cod.json",
            "ref_filename": "ref_09.json",
            "level": 3,
            "interacting_rules_count": 5,
            "rules_tested": [
                "R16: N:1 Strategy Pod Multiplexing & Order Ledger Ownership Tracking",
                "R17: Session Shield — Downstream POD_CRASH keeps upstream Venue FIX session ACTIVE during grace window",
                "R18: Inbound ExecutionReport (35=8, LeavesQty=0) during grace window marks order FILLED",
                "R19: POD_RECONNECT within grace window cancels pending synthetic Cancel-on-Disconnect timer",
                "R20: Grace timer expiration emits synthetic OrderCancelRequest (35=F) ONLY for remaining working orders of expired pod",
            ],
            "expert_rationale": (
                "pod-alpha crashes at t=10000ms (deadline 15000ms) and pod-beta at t=11000ms (deadline 16000ms). "
                "At t=12000ms, FG-alpha-01 is fully filled (leaves_qty=0). At t=14000ms, pod-beta reconnects. "
                "At t=15000ms, only FG-alpha-02 remains working for expired pod-alpha -> emits single 35=F for FG-alpha-02."
            ),
            "title": "Multiplexed Strategy Pod Faults & Upstream Order Lifecycle",
            "description": (
                "A gateway multiplexes two downstream strategy pods (pod-alpha and pod-beta) onto a single upstream "
                "venue session with pod_grace_timeout_ms=5000. Three working orders are initially active. Track the "
                "outbound venue messages across downstream pod disconnects, an upstream ExecutionReport (35=8), "
                "a pod reconnect, and a clock tick at t=15000ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "pod_grace_timeout_ms": 5000,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 20,
                "next_target_seq_num": 30,
                "last_sent_time_ms": 9000,
                "last_recv_time_ms": 9000,
                "outbound_messages_store": [],
                "working_orders": [
                    {"cl_ord_id": "FG-alpha-01", "pod_id": "pod-alpha", "leaves_qty": 100, "status": "WORKING"},
                    {"cl_ord_id": "FG-alpha-02", "pod_id": "pod-alpha", "leaves_qty": 50, "status": "WORKING"},
                    {"cl_ord_id": "FG-beta-01", "pod_id": "pod-beta", "leaves_qty": 200, "status": "WORKING"},
                ],
            },
            "inbound_event_trace": [
                {"event_type": "POD_CRASH", "timestamp_ms": 10000, "pod_id": "pod-alpha"},
                {"event_type": "POD_CRASH", "timestamp_ms": 11000, "pod_id": "pod-beta"},
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 12000,
                    "msg_type": "8",
                    "msg_seq_num": 30,
                    "cl_ord_id": "FG-alpha-01",
                    "exec_type": "F",
                    "leaves_qty": 0,
                    "send_time_ms": 12000,
                },
                {"event_type": "POD_RECONNECT", "timestamp_ms": 14000, "pod_id": "pod-beta"},
                {"event_type": "CLOCK_TICK", "timestamp_ms": 15000},
            ],
        },
        # ---------------------------------------------------------------------
        # TASK 10 (Level 3, k=5): Crypto Venue Lockout + ResetSeqNumFlag=Y Recovery
        # ---------------------------------------------------------------------
        {
            "task_id": "T10",
            "filename": "task_10_venue_reset_lockout.json",
            "ref_filename": "ref_10.json",
            "level": 3,
            "interacting_rules_count": 5,
            "rules_tested": [
                "R21: Unclean TCP_DROP without Logout triggers 2 * HeartBtInt (60000ms) SenderCompID lockout (status=LOCKED_OUT)",
                "R22: Premature RECONNECT_ATTEMPT during lockout window is rejected without advancing sequence numbers",
                "R23: Post-lockout RECONNECT_ATTEMPT with ResetSeqNumFlag=Y (141=Y) resets NextSenderMsgSeqNum and NextTargetMsgSeqNum to 1",
                "R24: Inbound Logon (35=A, 34=1, 141=Y) transitions session back to ACTIVE and clears disconnect_time_ms",
                "R5: Subsequent outbound NewOrderSingle (35=D) transmits at MsgSeqNum=2",
            ],
            "expert_rationale": (
                "Unclean TCP_DROP at t=100000ms locks out SenderCompID for 2 * 30000ms = 60000ms (until 160000ms). "
                "Reconnect at t=125000ms is ignored. Reconnect at t=161000ms with reset_seq_num_flag=True resets both "
                "sequences to 1 and emits Logon (34=1, 141=Y), followed by NewOrderSingle at 34=2."
            ),
            "title": "Unclean Transport Disconnect, Penalty Cooldown & Sequence Reset",
            "description": (
                "An active session with heart_bt_int_ms=30000 and unclean_drop_lockout_multiplier=2 experiences an "
                "unclean TCP_DROP at t=100000ms, followed by reconnection attempts at t=125000ms and t=161000ms, "
                "an inbound Logon at t=161100ms, and a new order submission at t=162000ms."
            ),
            "engine_config": {
                "heart_bt_int_ms": 30000,
                "unclean_drop_lockout_multiplier": 2,
                "send_redundant_resend_requests": False,
            },
            "initial_store": {
                "status": "ACTIVE",
                "next_sender_seq_num": 45,
                "next_target_seq_num": 60,
                "last_sent_time_ms": 95000,
                "last_recv_time_ms": 95000,
                "outbound_messages_store": [],
            },
            "inbound_event_trace": [
                {"event_type": "TCP_DROP", "timestamp_ms": 100000, "clean": False},
                {"event_type": "RECONNECT_ATTEMPT", "timestamp_ms": 125000, "reset_seq_num_flag": False},
                {"event_type": "RECONNECT_ATTEMPT", "timestamp_ms": 161000, "reset_seq_num_flag": True},
                {
                    "event_type": "RECV_MSG",
                    "timestamp_ms": 161100,
                    "msg_type": "A",
                    "msg_seq_num": 1,
                    "reset_seq_num_flag": True,
                    "send_time_ms": 161100,
                },
                {
                    "event_type": "SEND_ORDER",
                    "timestamp_ms": 162000,
                    "cl_ord_id": "ORD-POST-RESET-1",
                },
            ],
        },
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate or verify SessionBench tasks and reference outputs.")
    parser.add_argument("--verify", action="store_true", help="Verify existing tasks and references without overwriting.")
    args = parser.parse_args()

    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    REF_DIR.mkdir(parents=True, exist_ok=True)

    tasks = build_tasks()
    for raw_task in tasks:
        engine = FIX44SessionEngine(raw_task)
        ref_output = engine.run_trace(raw_task["inbound_event_trace"])
        violations = verify_fingate_invariants(raw_task, ref_output)
        if violations:
            raise RuntimeError(f"Invariant violation in {raw_task['task_id']}: {violations}")

        # Public task JSON given to models: strictly neutral, no spoilers or rule hints
        public_task = {
            "task_id": raw_task["task_id"],
            "filename": raw_task["filename"],
            "ref_filename": raw_task["ref_filename"],
            "level": raw_task["level"],
            "interacting_rules_count": raw_task["interacting_rules_count"],
            "title": raw_task["title"],
            "description": raw_task["description"],
            "engine_config": raw_task["engine_config"],
            "initial_store": raw_task["initial_store"],
            "inbound_event_trace": raw_task["inbound_event_trace"],
        }

        # Reference JSON used only by the grader & analysis scripts
        full_ref = {
            "task_id": raw_task["task_id"],
            "rules_tested": raw_task["rules_tested"],
            "expert_rationale": raw_task["expert_rationale"],
            "outbound_messages": ref_output["outbound_messages"],
            "final_state": ref_output["final_state"],
        }

        task_path = TASKS_DIR / raw_task["filename"]
        ref_path = REF_DIR / raw_task["ref_filename"]

        if args.verify:
            existing_ref = json.loads(ref_path.read_text())
            if (
                existing_ref["outbound_messages"] != full_ref["outbound_messages"]
                or existing_ref["final_state"] != full_ref["final_state"]
            ):
                raise AssertionError(f"Reference mismatch in {raw_task['task_id']}")
            print(f"[OK] Verified {raw_task['task_id']} ({raw_task['filename']}) -> {raw_task['ref_filename']} (0 invariant violations)")
        else:
            task_path.write_text(json.dumps(public_task, indent=2) + "\n")
            ref_path.write_text(json.dumps(full_ref, indent=2) + "\n")
            print(
                f"[GENERATED] {raw_task['task_id']} (Level {raw_task['level']}, k={raw_task['interacting_rules_count']} rules) "
                f"-> {len(full_ref['outbound_messages'])} outbound msgs, final_state={full_ref['final_state']}"
            )


if __name__ == "__main__":
    main()
