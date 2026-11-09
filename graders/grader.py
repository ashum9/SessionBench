"""
SessionBench Deterministic Code Grader.

Evaluates candidate model JSON outputs against engine-verified ground-truth references
using both strict binary `exact_pass` (0 or 1) and a 4-component `partial_score` (0.00 to 1.00):
  1. msg_type_sequence_score (25%): Correct sequence of outbound FIX MsgTypes (Tag 35)
  2. seq_num_and_flags_score (35%): Correct MsgSeqNum (34), PossDupFlag (43), GapFillFlag (123),
                                    NewSeqNo (36), BeginSeqNo (7), EndSeqNo (16), ClOrdID (11/41)
  3. final_state_score       (25%): Correct status, NextSenderMsgSeqNum, and NextTargetMsgSeqNum
  4. timing_and_reject_score (15%): Correct timestamps (+/- 5ms tolerance) and SessionRejectReason (373/45)
"""

from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Optional, Tuple

TIMESTAMP_TOLERANCE_MS = 5


def extract_json_payload(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extract a JSON object from a raw LLM response (handles markdown code fences or prose)."""
    if not raw_text:
        return None
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Look for ```json ... ``` blocks
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    for block in reversed(fenced):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and ("outbound_messages" in parsed or "final_state" in parsed):
                return parsed
        except Exception:
            continue

    # Fallback: find outermost { ... } containing "final_state"
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _within_ms(cand_val: Any, ref_val: Any, tol: int = TIMESTAMP_TOLERANCE_MS) -> bool:
    if ref_val is None and cand_val is None:
        return True
    if ref_val is None or cand_val is None:
        return False
    try:
        return abs(int(cand_val) - int(ref_val)) <= tol
    except (ValueError, TypeError):
        return False


def _norm_bool(val: Any, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().upper() in ("TRUE", "Y", "YES", "1")
    return bool(val)


def _check_seq_and_flags(cand_msg: Dict[str, Any], ref_msg: Dict[str, Any], idx: int) -> Tuple[bool, List[str]]:
    """Check sequence numbers, PossDup/GapFill flags, NewSeqNo, resend bounds, and order IDs."""
    reasons: List[str] = []

    if cand_msg.get("msg_seq_num") != ref_msg.get("msg_seq_num"):
        reasons.append(
            f"msg[{idx}] msg_seq_num mismatch: expected {ref_msg.get('msg_seq_num')}, got {cand_msg.get('msg_seq_num')}"
        )

    if _norm_bool(cand_msg.get("poss_dup_flag")) != _norm_bool(ref_msg.get("poss_dup_flag")):
        reasons.append(
            f"msg[{idx}] poss_dup_flag mismatch: expected {_norm_bool(ref_msg.get('poss_dup_flag'))}, "
            f"got {_norm_bool(cand_msg.get('poss_dup_flag'))}"
        )

    mtype = ref_msg.get("msg_type")
    if mtype == "4":
        if _norm_bool(cand_msg.get("gap_fill_flag")) != _norm_bool(ref_msg.get("gap_fill_flag")):
            reasons.append(
                f"msg[{idx}] gap_fill_flag mismatch: expected {_norm_bool(ref_msg.get('gap_fill_flag'))}, "
                f"got {_norm_bool(cand_msg.get('gap_fill_flag'))}"
            )
        if cand_msg.get("new_seq_no") != ref_msg.get("new_seq_no"):
            reasons.append(
                f"msg[{idx}] new_seq_no mismatch: expected {ref_msg.get('new_seq_no')}, got {cand_msg.get('new_seq_no')}"
            )

    if mtype == "2":
        if cand_msg.get("begin_seq_no") != ref_msg.get("begin_seq_no"):
            reasons.append(
                f"msg[{idx}] begin_seq_no mismatch: expected {ref_msg.get('begin_seq_no')}, got {cand_msg.get('begin_seq_no')}"
            )
        if cand_msg.get("end_seq_no") != ref_msg.get("end_seq_no"):
            reasons.append(
                f"msg[{idx}] end_seq_no mismatch: expected {ref_msg.get('end_seq_no')}, got {cand_msg.get('end_seq_no')}"
            )

    if "reset_seq_num_flag" in ref_msg:
        if _norm_bool(cand_msg.get("reset_seq_num_flag")) != _norm_bool(ref_msg.get("reset_seq_num_flag")):
            reasons.append(
                f"msg[{idx}] reset_seq_num_flag mismatch: expected {_norm_bool(ref_msg.get('reset_seq_num_flag'))}, "
                f"got {_norm_bool(cand_msg.get('reset_seq_num_flag'))}"
            )

    for key in ("cl_ord_id", "orig_cl_ord_id", "test_req_id"):
        if key in ref_msg and ref_msg[key] is not None:
            if cand_msg.get(key) != ref_msg[key]:
                reasons.append(f"msg[{idx}] {key} mismatch: expected {ref_msg[key]!r}, got {cand_msg.get(key)!r}")

    return (len(reasons) == 0), reasons


def _check_timing_and_reject(cand_msg: Dict[str, Any], ref_msg: Dict[str, Any], idx: int) -> Tuple[bool, List[str]]:
    """Check send_time_ms (+/- 5ms) and SessionRejectReason / RefSeqNum on 35=3."""
    reasons: List[str] = []
    if not _within_ms(cand_msg.get("send_time_ms"), ref_msg.get("send_time_ms")):
        reasons.append(
            f"msg[{idx}] send_time_ms mismatch: expected {ref_msg.get('send_time_ms')} (+/-{TIMESTAMP_TOLERANCE_MS}ms), "
            f"got {cand_msg.get('send_time_ms')}"
        )

    if ref_msg.get("msg_type") == "3":
        if cand_msg.get("ref_seq_num") != ref_msg.get("ref_seq_num"):
            reasons.append(
                f"msg[{idx}] ref_seq_num mismatch: expected {ref_msg.get('ref_seq_num')}, got {cand_msg.get('ref_seq_num')}"
            )
        if cand_msg.get("session_reject_reason") != ref_msg.get("session_reject_reason"):
            reasons.append(
                f"msg[{idx}] session_reject_reason mismatch: expected {ref_msg.get('session_reject_reason')}, "
                f"got {cand_msg.get('session_reject_reason')}"
            )

    return (len(reasons) == 0), reasons


def grade_prediction(candidate: Any, reference: Dict[str, Any]) -> Dict[str, Any]:
    """
    Grade a candidate output dictionary (or raw string) against the reference output.
    Returns:
      {
        "exact_pass": 0 or 1,
        "partial_score": float (0.0 to 1.0),
        "sub_scores": {
          "msg_type_sequence_score": float,
          "seq_num_and_flags_score": float,
          "final_state_score": float,
          "timing_and_reject_score": float
        },
        "diff_reasons": [str, ...]
      }
    """
    if isinstance(candidate, str):
        candidate = extract_json_payload(candidate)

    if not isinstance(candidate, dict):
        return {
            "exact_pass": 0,
            "partial_score": 0.0,
            "sub_scores": {
                "msg_type_sequence_score": 0.0,
                "seq_num_and_flags_score": 0.0,
                "final_state_score": 0.0,
                "timing_and_reject_score": 0.0,
            },
            "diff_reasons": ["Invalid or unparseable JSON output"],
        }

    cand_msgs = candidate.get("outbound_messages")
    cand_state = candidate.get("final_state")
    if not isinstance(cand_msgs, list) or not isinstance(cand_state, dict):
        return {
            "exact_pass": 0,
            "partial_score": 0.0,
            "sub_scores": {
                "msg_type_sequence_score": 0.0,
                "seq_num_and_flags_score": 0.0,
                "final_state_score": 0.0,
                "timing_and_reject_score": 0.0,
            },
            "diff_reasons": ["Missing required 'outbound_messages' list or 'final_state' object"],
        }

    ref_msgs: List[Dict[str, Any]] = reference["outbound_messages"]
    ref_state: Dict[str, Any] = reference["final_state"]
    diff_reasons: List[str] = []

    n_ref = len(ref_msgs)
    n_cand = len(cand_msgs)
    max_len = max(n_ref, n_cand, 1)

    if n_cand != n_ref:
        diff_reasons.append(f"outbound_messages count mismatch: expected {n_ref}, got {n_cand}")

    # 1. msg_type_sequence_score (25%)
    type_matches = 0
    for i in range(min(n_ref, n_cand)):
        c_m = cand_msgs[i] if isinstance(cand_msgs[i], dict) else {}
        if str(c_m.get("msg_type")) == str(ref_msgs[i].get("msg_type")):
            type_matches += 1
        else:
            diff_reasons.append(
                f"msg[{i}] msg_type mismatch: expected {ref_msgs[i].get('msg_type')!r}, got {c_m.get('msg_type')!r}"
            )
    msg_type_sequence_score = type_matches / max_len

    # 2. seq_num_and_flags_score (35%)
    seq_flag_matches = 0
    for i in range(min(n_ref, n_cand)):
        c_m = cand_msgs[i] if isinstance(cand_msgs[i], dict) else {}
        ok, reasons = _check_seq_and_flags(c_m, ref_msgs[i], i)
        if ok and str(c_m.get("msg_type")) == str(ref_msgs[i].get("msg_type")):
            seq_flag_matches += 1
        else:
            diff_reasons.extend(reasons)
    seq_num_and_flags_score = seq_flag_matches / max_len

    # 3. final_state_score (25%)
    state_points = 0
    if cand_state.get("status") == ref_state.get("status"):
        state_points += 1
    else:
        diff_reasons.append(
            f"final_state.status mismatch: expected {ref_state.get('status')!r}, got {cand_state.get('status')!r}"
        )

    if cand_state.get("next_sender_seq_num") == ref_state.get("next_sender_seq_num"):
        state_points += 1
    else:
        diff_reasons.append(
            f"final_state.next_sender_seq_num mismatch: expected {ref_state.get('next_sender_seq_num')}, "
            f"got {cand_state.get('next_sender_seq_num')}"
        )

    if cand_state.get("next_target_seq_num") == ref_state.get("next_target_seq_num"):
        state_points += 1
    else:
        diff_reasons.append(
            f"final_state.next_target_seq_num mismatch: expected {ref_state.get('next_target_seq_num')}, "
            f"got {cand_state.get('next_target_seq_num')}"
        )
    final_state_score = state_points / 3.0

    # 4. timing_and_reject_score (15%)
    timing_checks_total = max_len + 1  # +1 for final_state.disconnect_time_ms
    timing_matches = 0
    for i in range(min(n_ref, n_cand)):
        c_m = cand_msgs[i] if isinstance(cand_msgs[i], dict) else {}
        ok, reasons = _check_timing_and_reject(c_m, ref_msgs[i], i)
        if ok:
            timing_matches += 1
        else:
            diff_reasons.extend(reasons)

    if _within_ms(cand_state.get("disconnect_time_ms"), ref_state.get("disconnect_time_ms")):
        timing_matches += 1
    else:
        diff_reasons.append(
            f"final_state.disconnect_time_ms mismatch: expected {ref_state.get('disconnect_time_ms')}, "
            f"got {cand_state.get('disconnect_time_ms')}"
        )
    timing_and_reject_score = timing_matches / timing_checks_total

    partial_score = round(
        0.25 * msg_type_sequence_score
        + 0.35 * seq_num_and_flags_score
        + 0.25 * final_state_score
        + 0.15 * timing_and_reject_score,
        4,
    )

    exact_pass = 1 if len(diff_reasons) == 0 else 0

    return {
        "exact_pass": exact_pass,
        "partial_score": partial_score,
        "sub_scores": {
            "msg_type_sequence_score": round(msg_type_sequence_score, 4),
            "seq_num_and_flags_score": round(seq_num_and_flags_score, 4),
            "final_state_score": round(final_state_score, 4),
            "timing_and_reject_score": round(timing_and_reject_score, 4),
        },
        "diff_reasons": diff_reasons,
    }
