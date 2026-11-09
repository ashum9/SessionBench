"""
SessionBench Agentic Code-Repair Track — Candidate FIX 4.4 Engine (Seeded with 5 Real Production Bugs).

In the Code-Repair evaluation mode (analogous to SWE-bench for low-latency financial systems),
an AI coding agent is given this file (`code_repair/broken_engine.py`) and must patch the
state-machine bugs so that `python3 code_repair/evaluate_repair.py` passes 100/100 seeded
procedural chaos traces with 0 FinGate invariant violations.

Seeded Production Bugs in `CandidateFIXEngine`:
1. [Bug #1 - quickfix#778]: Uncoalesced Admin Gap-Fill during ResendRequest (emits one 35=4 per admin msg).
2. [Bug #2 - Off-by-one NewSeqNo]: Sets NewSeqNo (36) = seq instead of last_admin_seq + 1.
3. [Bug #3 - Admin Misclassification]: Omits '3' (SessionReject) from ADMIN_MSG_TYPES, causing 35=3 to be
   re-sent verbatim with PossDupFlag=Y (violating FinGate Invariant 4).
4. [Bug #4 - Out-of-Order GapFill Jump]: Processes 35=4 (123=Y) immediately even when 34 > NextTargetMsgSeqNum,
   swallowing the preceding sequence gap instead of queueing and requesting resend.
5. [Bug #5 - Garbled Frame Sequence Advance]: Fails to ignore garbled wire frames (corrupted BodyLength 9 or
   CheckSum 10), advancing NextTargetMsgSeqNum on corrupted bytes.
"""

from __future__ import annotations
from copy import deepcopy
from typing import Any, Dict, List, Optional

# BUG #3: '3' (SessionReject) is missing from ADMIN_MSG_TYPES!
BUGGY_ADMIN_MSG_TYPES = {"0", "1", "2", "4", "5", "A"}


class CandidateFIXEngine:
    """Broken FIX 4.4 + Session Shield state machine for Agentic Code-Repair evaluation."""

    def __init__(self, task_spec: Dict[str, Any]) -> None:
        self.config: Dict[str, Any] = deepcopy(task_spec["engine_config"])
        init: Dict[str, Any] = deepcopy(task_spec["initial_store"])

        self.status: str = init.get("status", "ACTIVE")
        self.next_sender_seq_num: int = int(init["next_sender_seq_num"])
        self.next_target_seq_num: int = int(init["next_target_seq_num"])
        self.disconnect_time_ms: Optional[int] = init.get("disconnect_time_ms", None)

        self.outbound_store: Dict[int, Dict[str, Any]] = {}
        for item in init.get("outbound_messages_store", []):
            self.outbound_store[int(item["msg_seq_num"])] = deepcopy(item)

        self.last_sent_time_ms: int = int(init.get("last_sent_time_ms", 0))
        self.last_recv_time_ms: int = int(init.get("last_recv_time_ms", 0))
        self.test_request_pending_since_ms: Optional[int] = init.get("test_request_pending_since_ms", None)

        self.resend_request_active: bool = bool(init.get("resend_request_active", False))
        self.inbound_queue: Dict[int, Dict[str, Any]] = {}

        self.working_orders: Dict[str, Dict[str, Any]] = {}
        for order in init.get("working_orders", []):
            self.working_orders[order["cl_ord_id"]] = deepcopy(order)
        self.crashed_pods_deadline_ms: Dict[str, int] = {}
        self.lockout_until_ms: Optional[int] = init.get("lockout_until_ms", None)
        self.emitted_messages: List[Dict[str, Any]] = []

    def _emit(self, msg: Dict[str, Any], advance_seq: bool = True) -> None:
        if advance_seq:
            seq = self.next_sender_seq_num
            self.next_sender_seq_num += 1
            msg["msg_seq_num"] = seq
            msg["poss_dup_flag"] = False
            self.outbound_store[seq] = deepcopy(msg)
        self.last_sent_time_ms = int(msg["send_time_ms"])
        self.emitted_messages.append(msg)

    def _execute_resend_range(self, begin_seq: int, end_seq: int, now_ms: int) -> None:
        actual_end = (self.next_sender_seq_num - 1) if end_seq == 0 else min(end_seq, self.next_sender_seq_num - 1)
        for seq in range(begin_seq, actual_end + 1):
            stored = self.outbound_store.get(seq)
            msg_type = stored["msg_type"] if stored else "0"
            # BUG #1, #2, #3: Does not coalesce contiguous admin messages, uses BUGGY_ADMIN_MSG_TYPES,
            # and sets new_seq_no = seq (off-by-one) on the first admin message!
            if msg_type in BUGGY_ADMIN_MSG_TYPES:
                self._emit(
                    {
                        "msg_type": "4",
                        "msg_seq_num": seq,
                        "poss_dup_flag": True,
                        "gap_fill_flag": True,
                        "new_seq_no": seq,  # BUG #2: Off-by-one NewSeqNo (not > seq)
                        "send_time_ms": now_ms,
                    },
                    advance_seq=False,
                )
            else:
                resent = {
                    "msg_type": msg_type,
                    "msg_seq_num": seq,
                    "poss_dup_flag": True,
                    "send_time_ms": now_ms,
                }
                if stored and "cl_ord_id" in stored:
                    resent["cl_ord_id"] = stored["cl_ord_id"]
                self._emit(resent, advance_seq=False)

    def step(self, event: Dict[str, Any]) -> None:
        ev_type = event["event_type"]
        now_ms = int(event["timestamp_ms"])

        if ev_type == "CLOCK_TICK":
            self._handle_clock_tick(now_ms)
        elif ev_type == "POD_CRASH":
            grace_ms = int(self.config.get("pod_grace_timeout_ms", 5000))
            self.crashed_pods_deadline_ms[event["pod_id"]] = now_ms + grace_ms
        elif ev_type == "POD_RECONNECT":
            self.crashed_pods_deadline_ms.pop(event["pod_id"], None)
        elif ev_type == "TCP_DROP":
            self.disconnect_time_ms = now_ms
            self.status = "DISCONNECTED"
        elif ev_type == "RECONNECT_ATTEMPT":
            reset_flag = bool(event.get("reset_seq_num_flag", False))
            if reset_flag:
                self.next_sender_seq_num = 1
                self.next_target_seq_num = 1
            self._emit(
                {"msg_type": "A", "reset_seq_num_flag": reset_flag, "send_time_ms": now_ms},
                advance_seq=True,
            )
        elif ev_type == "SEND_ORDER":
            if self.status == "ACTIVE":
                self._emit(
                    {"msg_type": "D", "cl_ord_id": event["cl_ord_id"], "send_time_ms": now_ms},
                    advance_seq=True,
                )
        elif ev_type == "RECV_MSG":
            self._handle_recv_msg(event, now_ms)

    def _handle_clock_tick(self, now_ms: int) -> None:
        expired_pods = [
            pod_id
            for pod_id, deadline in sorted(self.crashed_pods_deadline_ms.items())
            if now_ms >= deadline
        ]
        for pod_id in expired_pods:
            del self.crashed_pods_deadline_ms[pod_id]
            for cl_ord_id, order in list(self.working_orders.items()):
                if order["pod_id"] == pod_id and int(order["leaves_qty"]) > 0:
                    order["leaves_qty"] = 0
                    order["status"] = "CANCELLING"
                    self._emit(
                        {
                            "msg_type": "F",
                            "cl_ord_id": f"CANCEL-{cl_ord_id}",
                            "orig_cl_ord_id": cl_ord_id,
                            "send_time_ms": now_ms,
                        },
                        advance_seq=True,
                    )

        if self.status not in ("ACTIVE", "RESENDING"):
            return
        hb_ms = int(self.config.get("heart_bt_int_ms", 30000))
        if (now_ms - self.last_sent_time_ms) >= hb_ms:
            self._emit({"msg_type": "0", "send_time_ms": now_ms}, advance_seq=True)

    def _handle_recv_msg(self, msg: Dict[str, Any], now_ms: int) -> None:
        if self.status == "DISCONNECTED" and msg["msg_type"] != "A":
            return

        # BUG #5: Does not check `msg.get("garbled")` or validate `msg.get("raw_wire")`!
        # Processes garbled messages as valid and increments NextTargetMsgSeqNum.

        self.last_recv_time_ms = now_ms
        msg_type = msg["msg_type"]
        seq_num = int(msg["msg_seq_num"])
        poss_dup = bool(msg.get("poss_dup_flag", False))

        # BUG #4: Processes ANY 35=4 (including 123=Y GapFill) immediately even if seq_num > next_target_seq_num!
        if msg_type == "4":
            self.next_target_seq_num = int(msg["new_seq_no"])
            self.status = "ACTIVE"
            return

        if seq_num < self.next_target_seq_num:
            if poss_dup:
                return
            self._emit({"msg_type": "5", "send_time_ms": now_ms}, advance_seq=True)
            self.status = "DISCONNECTED"
            self.disconnect_time_ms = now_ms
            return

        if poss_dup and ("orig_sending_time_ms" not in msg or msg["orig_sending_time_ms"] is None):
            if seq_num == self.next_target_seq_num:
                self.next_target_seq_num += 1
            self._emit(
                {
                    "msg_type": "3",
                    "ref_seq_num": seq_num,
                    "session_reject_reason": 1,
                    "send_time_ms": now_ms,
                },
                advance_seq=True,
            )
            return

        if seq_num > self.next_target_seq_num:
            self._emit(
                {
                    "msg_type": "2",
                    "begin_seq_no": self.next_target_seq_num,
                    "end_seq_no": 0,
                    "send_time_ms": now_ms,
                },
                advance_seq=True,
            )
            self.status = "RESENDING"
            return

        self.next_target_seq_num += 1
        if msg_type == "A":
            self.status = "ACTIVE"
        elif msg_type == "1":
            resp: Dict[str, Any] = {"msg_type": "0", "send_time_ms": now_ms}
            if msg.get("test_req_id") is not None:
                resp["test_req_id"] = msg["test_req_id"]
            self._emit(resp, advance_seq=True)
        elif msg_type == "2":
            self._execute_resend_range(int(msg["begin_seq_no"]), int(msg["end_seq_no"]), now_ms)
        elif msg_type == "8":
            cl_ord_id = msg.get("cl_ord_id")
            if cl_ord_id and cl_ord_id in self.working_orders:
                self.working_orders[cl_ord_id]["leaves_qty"] = int(msg.get("leaves_qty", 0))
                if int(msg.get("leaves_qty", 0)) == 0:
                    self.working_orders[cl_ord_id]["status"] = "FILLED"

    def run_trace(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        for ev in events:
            self.step(ev)
        return {
            "outbound_messages": self.emitted_messages,
            "final_state": {
                "status": self.status,
                "next_sender_seq_num": self.next_sender_seq_num,
                "next_target_seq_num": self.next_target_seq_num,
                "disconnect_time_ms": self.disconnect_time_ms,
            },
        }
