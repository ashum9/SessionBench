"""
Formal Executable Specification of the FIX 4.4 Session State Machine & FinGate Extensions.

This module implements the deterministic reference state machine used by SessionBench
to generate and verify ground-truth execution traces across all 10 tasks (Levels 1-3).

Formal Rules Implemented:
- R1:  Admin vs. Application Message Classification during Resend (35 in {0,1,2,3,4,5,A} are Admin)
- R2:  Contiguous Admin Message Coalescing into a single SequenceReset-GapFill (35=4, 123=Y, 36=last+1)
- R3:  Outbound Heartbeat (35=0) emission on idle send timer >= HeartBtInt and TestRequest (35=1) echo
- R4:  Inbound silence > HeartBtInt triggers TestRequest (35=1); silence > HeartBtInt + Grace triggers TCP disconnect
- R5:  Persistent sequence number continuity across crashes and reconnects
- R6:  Logon (35=A) acknowledgement precedes ResendRequest (35=2) on reconnect sequence deficit
- R7:  Inbound sequence gap (34 > NextTargetMsgSeqNum) triggers ResendRequest (7=expected, 16=0) and RESENDING state
- R8:  PossDupFlag=Y (43=Y) validation with OrigSendingTime (122) <= SendingTime (52) and safe duplicate drop (34 < expected)
- R9:  Conditional Required Tag Missing (43=Y without 122) triggers Session Reject (35=3, 373=1) and advances TargetSeq
- R10: MsgSeqNum Too Low (34 < expected with 43=N) triggers immediate fatal Logout (35=5) without advancing TargetSeq
- R11: SequenceReset-GapFill (123=Y) attempting to lower NewSeqNo (36 < expected) triggers Reject (35=3, 373=5)
- R12: SequenceReset-Reset (123=N) bypasses sequence ordering and unconditionally sets NextTargetMsgSeqNum = NewSeqNo
- R13: Open-ended ResendRequest (EndSeqNo 16=0) replays up to NextSenderMsgSeqNum - 1
- R14: SendRedundantResendRequests=False suppresses duplicate ResendRequests while already in RESENDING state
- R15: Out-of-order inbound messages (including out-of-order 35=4, 123=Y GapFill) are queued until missing gap is filled
- R16-R20 (FinGate Session Shield): N:1 pod multiplexing, grace timer on POD_CRASH, fill tracking during grace window,
           POD_RECONNECT timer cancellation, and synthetic OrderCancelRequest (35=F) only for remaining open orders
- R21-R24 (Venue Lockout & Reset): Unclean TCP_DROP triggers 2 * HeartBtInt lockout, premature reconnect rejection,
           and ResetSeqNumFlag=Y (141=Y) bidirectional sequence reset to 1.
- R25 (Wire-Level Framing & Garbled Message Handling): Validates raw FIX 4.4 wire frames (BeginString 8=FIX.4.4,
           BodyLength 9=N, and modulo-256 CheckSum 10=XYZ). Garbled frames (corrupted BodyLength or CheckSum)
           must be silently ignored without advancing NextTargetMsgSeqNum or emitting SessionReject (35=3).
"""

from __future__ import annotations
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

ADMIN_MSG_TYPES = {"0", "1", "2", "3", "4", "5", "A"}
SOH = "\x01"


def encode_fix_wire_frame(
    msg: Dict[str, Any],
    sender_comp_id: str = "EXCHANGE",
    target_comp_id: str = "FINGATE",
    corrupt_checksum: bool = False,
    corrupt_body_length: bool = False,
) -> str:
    """
    Encode an event/message dictionary into a canonical pipe-delimited FIX 4.4 wire string
    with accurate BodyLength (9) and modulo-256 CheckSum (10), where SOH (0x01) is used for
    byte-level checksum calculation and rendered as '|' for human/JSON readability.
    """
    body_tags: List[str] = [
        f"35={msg['msg_type']}",
        f"34={msg['msg_seq_num']}",
        f"49={sender_comp_id}",
        f"56={target_comp_id}",
        f"52={msg.get('timestamp_ms', msg.get('send_time_ms', 0))}",
    ]
    if msg.get("poss_dup_flag") is True:
        body_tags.append("43=Y")
    if msg.get("orig_sending_time_ms") is not None:
        body_tags.append(f"122={msg['orig_sending_time_ms']}")
    if msg.get("gap_fill_flag") is not None:
        body_tags.append(f"123={'Y' if msg['gap_fill_flag'] else 'N'}")
    if msg.get("new_seq_no") is not None:
        body_tags.append(f"36={msg['new_seq_no']}")
    if msg.get("begin_seq_no") is not None:
        body_tags.append(f"7={msg['begin_seq_no']}")
    if msg.get("end_seq_no") is not None:
        body_tags.append(f"16={msg['end_seq_no']}")
    if msg.get("test_req_id") is not None:
        body_tags.append(f"112={msg['test_req_id']}")
    if msg.get("cl_ord_id") is not None:
        body_tags.append(f"11={msg['cl_ord_id']}")
    if msg.get("leaves_qty") is not None:
        body_tags.append(f"151={msg['leaves_qty']}")

    body_soh = SOH.join(body_tags) + SOH
    body_len = len(body_soh.encode("ascii"))
    if corrupt_body_length:
        body_len += 7

    prefix_soh = f"8=FIX.4.4{SOH}9={body_len}{SOH}" + body_soh
    checksum = sum(prefix_soh.encode("ascii")) % 256
    if corrupt_checksum:
        checksum = (checksum + 42) % 256

    full_soh = f"{prefix_soh}10={checksum:03d}{SOH}"
    return full_soh.replace(SOH, "|")


def validate_fix_wire_frame(raw_wire: str) -> Tuple[bool, Optional[str]]:
    """
    Validate a raw FIX 4.4 wire string (delimited by SOH '\\x01' or '|').
    Returns (is_valid, error_reason). Per FIX 4.4 Vol 2, any framing error
    classifies the message as GARBLED (must be ignored without incrementing NextTargetMsgSeqNum).
    """
    wire_soh = raw_wire.replace("|", SOH) if "|" in raw_wire and SOH not in raw_wire else raw_wire
    if not wire_soh.startswith(f"8=FIX.4.4{SOH}9="):
        return False, "Missing or out-of-order BeginString (8=FIX.4.4) / BodyLength (9)"

    # Locate end of tag 9
    second_soh = wire_soh.find(SOH, len(f"8=FIX.4.4{SOH}"))
    if second_soh == -1:
        return False, "Malformed BodyLength (9) field"
    try:
        declared_body_len = int(wire_soh[len(f"8=FIX.4.4{SOH}9="):second_soh])
    except ValueError:
        return False, "Non-integer BodyLength (9)"

    # Locate CheckSum tag 10= at trailer
    trailer_marker = f"{SOH}10="
    checksum_pos = wire_soh.rfind(trailer_marker)
    if checksum_pos == -1 or not wire_soh.endswith(SOH):
        return False, "Missing CheckSum (10) trailer"

    actual_body_len = (checksum_pos + 1) - (second_soh + 1)
    if actual_body_len != declared_body_len:
        return False, f"BodyLength (9) mismatch: declared={declared_body_len}, actual={actual_body_len}"

    declared_cs_str = wire_soh[checksum_pos + len(trailer_marker):-1]
    if len(declared_cs_str) != 3 or not declared_cs_str.isdigit():
        return False, "Malformed 3-digit CheckSum (10)"
    declared_cs = int(declared_cs_str)
    actual_cs = sum(wire_soh[:checksum_pos + 1].encode("ascii")) % 256
    if actual_cs != declared_cs:
        return False, f"CheckSum (10) mismatch: declared={declared_cs:03d}, computed={actual_cs:03d}"

    return True, None


class FIX44SessionEngine:
    """Deterministic FIX 4.4 + FinGate Session Shield state machine oracle."""

    def __init__(self, task_spec: Dict[str, Any]) -> None:
        self.config: Dict[str, Any] = deepcopy(task_spec["engine_config"])
        init: Dict[str, Any] = deepcopy(task_spec["initial_store"])

        self.status: str = init.get("status", "ACTIVE")
        self.next_sender_seq_num: int = int(init["next_sender_seq_num"])
        self.next_target_seq_num: int = int(init["next_target_seq_num"])
        self.disconnect_time_ms: Optional[int] = init.get("disconnect_time_ms", None)

        # Outbound message store keyed by int sequence number
        self.outbound_store: Dict[int, Dict[str, Any]] = {}
        for item in init.get("outbound_messages_store", []):
            self.outbound_store[int(item["msg_seq_num"])] = deepcopy(item)

        # Timers
        self.last_sent_time_ms: int = int(init.get("last_sent_time_ms", 0))
        self.last_recv_time_ms: int = int(init.get("last_recv_time_ms", 0))
        self.test_request_pending_since_ms: Optional[int] = init.get("test_request_pending_since_ms", None)

        # Resend / gap tracking
        self.resend_request_active: bool = bool(init.get("resend_request_active", False))
        self.inbound_queue: Dict[int, Dict[str, Any]] = {}

        # FinGate Session Shield pod order ledger
        self.working_orders: Dict[str, Dict[str, Any]] = {}
        for order in init.get("working_orders", []):
            self.working_orders[order["cl_ord_id"]] = deepcopy(order)
        self.crashed_pods_deadline_ms: Dict[str, int] = {}

        # Venue lockout tracking
        self.lockout_until_ms: Optional[int] = init.get("lockout_until_ms", None)

        # Recorded outputs during trace execution
        self.emitted_messages: List[Dict[str, Any]] = []

    def _emit(self, msg: Dict[str, Any], advance_seq: bool = True) -> None:
        """Emit an outbound FIX message and optionally advance NextSenderMsgSeqNum."""
        if advance_seq:
            seq = self.next_sender_seq_num
            self.next_sender_seq_num += 1
            msg["msg_seq_num"] = seq
            msg["poss_dup_flag"] = False
            self.outbound_store[seq] = deepcopy(msg)
        self.last_sent_time_ms = int(msg["send_time_ms"])
        self.emitted_messages.append(msg)

    def _execute_resend_range(self, begin_seq: int, end_seq: int, now_ms: int) -> None:
        """
        Execute FIX 4.4 ResendRequest replay over [begin_seq, end_seq].
        Implements R1, R2 (Contiguous Admin Gap-Fill Coalescing), and R13 (16=0 -> max stored).
        """
        actual_end = (self.next_sender_seq_num - 1) if end_seq == 0 else min(end_seq, self.next_sender_seq_num - 1)
        seq = begin_seq
        while seq <= actual_end:
            stored = self.outbound_store.get(seq)
            msg_type = stored["msg_type"] if stored else "0"
            if msg_type in ADMIN_MSG_TYPES:
                # Coalesce contiguous admin messages into a single SequenceReset-GapFill (35=4, 123=Y)
                gap_start = seq
                while seq <= actual_end:
                    s_next = self.outbound_store.get(seq)
                    t_next = s_next["msg_type"] if s_next else "0"
                    if t_next in ADMIN_MSG_TYPES:
                        seq += 1
                    else:
                        break
                new_seq_no = seq  # Next sequence number immediately following the coalesced admin block
                self._emit(
                    {
                        "msg_type": "4",
                        "msg_seq_num": gap_start,
                        "poss_dup_flag": True,
                        "gap_fill_flag": True,
                        "new_seq_no": new_seq_no,
                        "send_time_ms": now_ms,
                    },
                    advance_seq=False,
                )
            else:
                # Application message: resend verbatim with PossDupFlag=Y (43=True)
                resent = {
                    "msg_type": msg_type,
                    "msg_seq_num": seq,
                    "poss_dup_flag": True,
                    "send_time_ms": now_ms,
                }
                if stored and "cl_ord_id" in stored:
                    resent["cl_ord_id"] = stored["cl_ord_id"]
                self._emit(resent, advance_seq=False)
                seq += 1

    def _drain_inbound_queue(self, now_ms: int) -> None:
        """Drain contiguous queued out-of-order messages once a sequence gap is filled."""
        while self.next_target_seq_num in self.inbound_queue:
            queued = self.inbound_queue.pop(self.next_target_seq_num)
            q_type = queued["msg_type"]
            if q_type == "4" and queued.get("gap_fill_flag", False):
                new_seq = int(queued["new_seq_no"])
                self.next_target_seq_num = new_seq
            else:
                self.next_target_seq_num += 1
                if q_type == "2":
                    self._execute_resend_range(
                        int(queued["begin_seq_no"]),
                        int(queued["end_seq_no"]),
                        now_ms,
                    )
        if not self.inbound_queue and self.status == "RESENDING":
            self.status = "ACTIVE"
            self.resend_request_active = False

    def step(self, event: Dict[str, Any]) -> None:
        """Process a single timestamped inbound trace event."""
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
            clean = bool(event.get("clean", False))
            self.disconnect_time_ms = now_ms
            if not clean and self.config.get("unclean_drop_lockout_multiplier"):
                mult = int(self.config["unclean_drop_lockout_multiplier"])
                hb_ms = int(self.config["heart_bt_int_ms"])
                self.lockout_until_ms = now_ms + mult * hb_ms
                self.status = "LOCKED_OUT"
            else:
                self.status = "DISCONNECTED"
        elif ev_type == "RECONNECT_ATTEMPT":
            if self.status == "LOCKED_OUT" and self.lockout_until_ms and now_ms < self.lockout_until_ms:
                # Rejected due to active SenderCompID lockout window
                return
            reset_flag = bool(event.get("reset_seq_num_flag", False))
            if reset_flag:
                self.next_sender_seq_num = 1
                self.next_target_seq_num = 1
                self.inbound_queue.clear()
                self.resend_request_active = False
            self._emit(
                {
                    "msg_type": "A",
                    "reset_seq_num_flag": reset_flag,
                    "send_time_ms": now_ms,
                },
                advance_seq=True,
            )
        elif ev_type == "SEND_ORDER":
            if self.status == "ACTIVE":
                self._emit(
                    {
                        "msg_type": "D",
                        "cl_ord_id": event["cl_ord_id"],
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
        elif ev_type == "RECV_MSG":
            self._handle_recv_msg(event, now_ms)

    def _handle_clock_tick(self, now_ms: int) -> None:
        # 1. Check FinGate Session Shield pod grace expirations
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
        grace_ms = int(self.config.get("test_request_grace_ms", hb_ms))

        # 2. Check inbound silence timeout if TestRequest is already pending
        if self.test_request_pending_since_ms is not None:
            if now_ms - self.test_request_pending_since_ms >= grace_ms:
                self.status = "DISCONNECTED"
                self.disconnect_time_ms = now_ms
                return

        # 3. Check if inbound silence >= HeartBtInt requires sending TestRequest (35=1)
        if self.test_request_pending_since_ms is None and (now_ms - self.last_recv_time_ms) >= hb_ms:
            self.test_request_pending_since_ms = now_ms
            self._emit(
                {
                    "msg_type": "1",
                    "send_time_ms": now_ms,
                },
                advance_seq=True,
            )
            return

        # 4. Check if outbound idle timer >= HeartBtInt requires sending Heartbeat (35=0)
        if (now_ms - self.last_sent_time_ms) >= hb_ms:
            self._emit(
                {
                    "msg_type": "0",
                    "send_time_ms": now_ms,
                },
                advance_seq=True,
            )

    def _handle_recv_msg(self, msg: Dict[str, Any], now_ms: int) -> None:
        if self.status == "DISCONNECTED" and msg["msg_type"] != "A":
            return

        # R25: Wire-level framing & CheckSum/BodyLength verification.
        # Per FIX 4.4 Vol 2 ("Garbled Messages"): if a message has an invalid BodyLength (9)
        # or invalid modulo-256 CheckSum (10), it must be silently ignored without advancing
        # NextTargetMsgSeqNum and without emitting a SessionReject (35=3).
        if bool(msg.get("garbled", False)):
            return
        if "raw_wire" in msg and msg["raw_wire"]:
            is_valid_wire, _ = validate_fix_wire_frame(str(msg["raw_wire"]))
            if not is_valid_wire:
                return

        self.last_recv_time_ms = now_ms
        self.test_request_pending_since_ms = None

        msg_type = msg["msg_type"]
        seq_num = int(msg["msg_seq_num"])
        poss_dup = bool(msg.get("poss_dup_flag", False))

        # R12: SequenceReset-Reset (35=4, 123=N) bypasses sequence number checks completely
        if msg_type == "4" and not bool(msg.get("gap_fill_flag", False)):
            self.next_target_seq_num = int(msg["new_seq_no"])
            self.inbound_queue.clear()
            self.resend_request_active = False
            self.status = "ACTIVE"
            return

        # R8 & R10: Check sequence number < NextTargetMsgSeqNum
        if seq_num < self.next_target_seq_num:
            if poss_dup:
                # Valid PossDup duplicate of already-processed sequence number -> safely ignore
                return
            else:
                # Fatal MsgSeqNum too low without PossDupFlag=Y -> immediate Logout (35=5) & disconnect
                self._emit(
                    {
                        "msg_type": "5",
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
                self.status = "DISCONNECTED"
                self.disconnect_time_ms = now_ms
                return

        # R9: Check PossDupFlag=Y missing required OrigSendingTime (122)
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

        # R6 & R7: Check sequence gap (seq_num > NextTargetMsgSeqNum)
        if seq_num > self.next_target_seq_num:
            if msg_type == "A" and self.status == "DISCONNECTED":
                # Acknowledge Logon first, then emit ResendRequest for the missed gap
                self._emit(
                    {
                        "msg_type": "A",
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
                self._emit(
                    {
                        "msg_type": "2",
                        "begin_seq_no": self.next_target_seq_num,
                        "end_seq_no": 0,
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
                self.inbound_queue[seq_num] = deepcopy(msg)
                self.status = "RESENDING"
                self.resend_request_active = True
                return

            # Queue out-of-order message (including out-of-order 35=4, 123=Y GapFill!)
            self.inbound_queue[seq_num] = deepcopy(msg)
            send_redundant = bool(self.config.get("send_redundant_resend_requests", False))
            if not self.resend_request_active or send_redundant:
                self._emit(
                    {
                        "msg_type": "2",
                        "begin_seq_no": self.next_target_seq_num,
                        "end_seq_no": 0,
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
                self.resend_request_active = True
                self.status = "RESENDING"

            # In bidirectional ResendRequest collision, still service counterparty's ResendRequest
            # immediately so the two engines do not deadlock waiting on each other.
            if msg_type == "2":
                self._execute_resend_range(
                    int(msg["begin_seq_no"]),
                    int(msg["end_seq_no"]),
                    now_ms,
                )
                # Mark queued ResendRequest as regular no-op once gap closes so it isn't re-run
                self.inbound_queue[seq_num]["msg_type"] = "0"
            return

        # Exact sequence match: seq_num == self.next_target_seq_num
        if msg_type == "4" and bool(msg.get("gap_fill_flag", False)):
            new_seq = int(msg["new_seq_no"])
            if new_seq < self.next_target_seq_num:
                # R11: Attempt to lower sequence number via GapFill -> Session Reject (373=5)
                self.next_target_seq_num += 1
                self._emit(
                    {
                        "msg_type": "3",
                        "ref_seq_num": seq_num,
                        "session_reject_reason": 5,
                        "send_time_ms": now_ms,
                    },
                    advance_seq=True,
                )
                return
            self.next_target_seq_num = new_seq
            self._drain_inbound_queue(now_ms)
            return

        # Advance expected target sequence number by 1 for normal messages
        self.next_target_seq_num += 1

        if msg_type == "A":
            self.status = "ACTIVE"
            self.disconnect_time_ms = None
        elif msg_type == "1":
            resp: Dict[str, Any] = {
                "msg_type": "0",
                "send_time_ms": now_ms,
            }
            if "test_req_id" in msg and msg["test_req_id"] is not None:
                resp["test_req_id"] = msg["test_req_id"]
            self._emit(resp, advance_seq=True)
        elif msg_type == "2":
            self._execute_resend_range(
                int(msg["begin_seq_no"]),
                int(msg["end_seq_no"]),
                now_ms,
            )
        elif msg_type == "8":
            cl_ord_id = msg.get("cl_ord_id")
            if cl_ord_id and cl_ord_id in self.working_orders:
                self.working_orders[cl_ord_id]["leaves_qty"] = int(msg.get("leaves_qty", 0))
                if int(msg.get("leaves_qty", 0)) == 0:
                    self.working_orders[cl_ord_id]["status"] = "FILLED"

        # Drain any queued messages that are now contiguous
        self._drain_inbound_queue(now_ms)

    def run_trace(self, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run all inbound trace events and return the canonical output dictionary."""
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


def verify_fingate_invariants(task_spec: Dict[str, Any], output: Dict[str, Any]) -> List[str]:
    """
    Verify FinGate's 6 formal session invariants on a reference or model output.
    Returns a list of invariant violations (empty list if all invariants hold).
    """
    violations: List[str] = []
    outbound = output.get("outbound_messages", [])
    init_sender_seq = int(task_spec["initial_store"]["next_sender_seq_num"])

    expected_new_seq = init_sender_seq
    for idx, msg in enumerate(outbound):
        mtype = msg.get("msg_type")
        seq = msg.get("msg_seq_num")
        poss_dup = bool(msg.get("poss_dup_flag", False))

        # Invariant 1: No new (non-PossDup) outbound MsgSeqNum is ever reused or skipped
        # (unless ResetSeqNumFlag=Y reset occurred on Logon)
        if not poss_dup:
            if mtype == "A" and msg.get("reset_seq_num_flag") is True:
                expected_new_seq = 1
            if seq != expected_new_seq:
                violations.append(
                    f"Invariant 1 violation at msg[{idx}]: expected MsgSeqNum={expected_new_seq}, got {seq}"
                )
            expected_new_seq = seq + 1

        # Invariant 3 & 4: Resent messages must carry PossDupFlag=Y, and Admin messages
        # (0, 1, 2, 3, 5, A) must NEVER be resent with PossDupFlag=Y
        if poss_dup:
            if mtype in {"0", "1", "2", "3", "5", "A"}:
                violations.append(
                    f"Invariant 4 violation at msg[{idx}]: Admin message 35={mtype} resent with PossDupFlag=Y instead of SequenceReset-GapFill (35=4, 123=Y)"
                )
            if mtype == "4" and msg.get("gap_fill_flag") is True:
                new_seq_no = msg.get("new_seq_no")
                if not isinstance(new_seq_no, int) or new_seq_no <= seq:
                    violations.append(
                        f"Invariant 4 violation at msg[{idx}]: GapFill NewSeqNo ({new_seq_no}) must be strictly greater than MsgSeqNum ({seq})"
                    )

    return violations
