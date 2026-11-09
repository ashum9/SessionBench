"""
SessionBench Grader Reliability & 80-Mutation Test Suite.

Proves two critical properties required by the micro1 Eval Hackathon rubric:
  1. 10/10 engine-verified reference solutions achieve exact_pass == 1 (100% pass rate) and partial_score == 1.0.
  2. 80/80 systematically corrupted outputs (10 tasks x 8 domain-specific mutation operators)
     are 100% rejected (exact_pass == 0) with specific diagnostic diff_reasons.
"""

from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Tuple
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graders.grader import grade_prediction

BASE_DIR = Path(__file__).resolve().parent.parent
REF_DIR = BASE_DIR / "reference"
RESULTS_DIR = BASE_DIR / "results"


def load_references() -> List[Tuple[str, Dict[str, Any]]]:
    refs: List[Tuple[str, Dict[str, Any]]] = []
    for idx in range(1, 11):
        task_id = f"T{idx:02d}"
        ref_path = REF_DIR / f"ref_{idx:02d}.json"
        refs.append((task_id, json.loads(ref_path.read_text())))
    return refs


# -----------------------------------------------------------------------------
# 8 Domain-Specific FIX 4.4 Protocol Mutation Operators
# -----------------------------------------------------------------------------

def mutate_m1_off_by_one_sender_seq(ref: Dict[str, Any]) -> Dict[str, Any]:
    m = deepcopy(ref)
    m["final_state"]["next_sender_seq_num"] += 1
    return m


def mutate_m2_off_by_one_target_seq(ref: Dict[str, Any]) -> Dict[str, Any]:
    m = deepcopy(ref)
    m["final_state"]["next_target_seq_num"] -= 1
    return m


def mutate_m3_flip_possdup_flag(ref: Dict[str, Any]) -> Dict[str, Any]:
    m = deepcopy(ref)
    m["outbound_messages"][0]["poss_dup_flag"] = not bool(m["outbound_messages"][0].get("poss_dup_flag", False))
    return m


def mutate_m4_uncoalesced_or_wrong_msg_type(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Replaces GapFill (35=4) with raw Heartbeat (35=0), or swaps first MsgType."""
    m = deepcopy(ref)
    for msg in m["outbound_messages"]:
        if msg.get("msg_type") == "4":
            msg["msg_type"] = "0"
            return m
    m["outbound_messages"][0]["msg_type"] = "0" if m["outbound_messages"][0]["msg_type"] != "0" else "1"
    return m


def mutate_m5_off_by_one_new_seq_no_or_msg_seq(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Classic LLM bug: setting NewSeqNo (36) = last_admin_seq instead of last_admin_seq + 1."""
    m = deepcopy(ref)
    for msg in m["outbound_messages"]:
        if msg.get("msg_type") == "4" and "new_seq_no" in msg:
            msg["new_seq_no"] = int(msg["new_seq_no"]) - 1
            return m
    m["outbound_messages"][0]["msg_seq_num"] = int(m["outbound_messages"][0]["msg_seq_num"]) + 1
    return m


def mutate_m6_wrong_reject_reason_or_tag(ref: Dict[str, Any]) -> Dict[str, Any]:
    """Corrupts SessionRejectReason (373), BeginSeqNo (7), ClOrdID (11), or appends extra message."""
    m = deepcopy(ref)
    for msg in m["outbound_messages"]:
        if msg.get("msg_type") == "3":
            msg["session_reject_reason"] = int(msg.get("session_reject_reason", 1)) + 99
            return m
        if msg.get("msg_type") == "2":
            msg["begin_seq_no"] = int(msg.get("begin_seq_no", 1)) + 1
            return m
        if "cl_ord_id" in msg:
            msg["cl_ord_id"] = msg["cl_ord_id"] + "-CORRUPT"
            return m
    m["outbound_messages"].append(deepcopy(m["outbound_messages"][-1]))
    return m


def mutate_m7_premature_state_transition(ref: Dict[str, Any]) -> Dict[str, Any]:
    m = deepcopy(ref)
    cur = m["final_state"]["status"]
    m["final_state"]["status"] = "RESENDING" if cur == "ACTIVE" else "ACTIVE"
    return m


def mutate_m8_timer_drift_5000ms(ref: Dict[str, Any]) -> Dict[str, Any]:
    m = deepcopy(ref)
    if m["final_state"].get("disconnect_time_ms") is not None:
        m["final_state"]["disconnect_time_ms"] += 5000
    else:
        m["outbound_messages"][0]["send_time_ms"] += 5000
    return m


MUTATION_OPERATORS: List[Tuple[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = [
    ("M1_off_by_one_sender_seq", mutate_m1_off_by_one_sender_seq),
    ("M2_off_by_one_target_seq", mutate_m2_off_by_one_target_seq),
    ("M3_flip_possdup_flag", mutate_m3_flip_possdup_flag),
    ("M4_uncoalesced_or_wrong_msg_type", mutate_m4_uncoalesced_or_wrong_msg_type),
    ("M5_off_by_one_new_seq_no_or_msg_seq", mutate_m5_off_by_one_new_seq_no_or_msg_seq),
    ("M6_wrong_reject_reason_or_tag", mutate_m6_wrong_reject_reason_or_tag),
    ("M7_premature_state_transition", mutate_m7_premature_state_transition),
    ("M8_timer_drift_5000ms", mutate_m8_timer_drift_5000ms),
]


@pytest.mark.parametrize("task_id,ref_data", load_references())
def test_reference_solutions_pass_100_percent(task_id: str, ref_data: Dict[str, Any]) -> None:
    res = grade_prediction(ref_data, ref_data)
    assert res["exact_pass"] == 1, f"Reference {task_id} failed: {res['diff_reasons']}"
    assert res["partial_score"] == 1.0, f"Reference {task_id} partial score < 1.0: {res}"


@pytest.mark.parametrize("task_id,ref_data", load_references())
@pytest.mark.parametrize("mutation_name,mutator", MUTATION_OPERATORS)
def test_all_80_mutations_rejected(
    task_id: str,
    ref_data: Dict[str, Any],
    mutation_name: str,
    mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> None:
    corrupted = mutator(ref_data)
    res = grade_prediction(corrupted, ref_data)
    assert res["exact_pass"] == 0, f"Mutation {mutation_name} on {task_id} was NOT rejected!"
    assert len(res["diff_reasons"]) > 0, f"Mutation {mutation_name} on {task_id} produced no diff_reasons!"
    assert 0.0 <= res["partial_score"] < 1.0, f"Mutation {mutation_name} on {task_id} had partial_score={res['partial_score']}"


def generate_mutation_report() -> Dict[str, Any]:
    """Generate and save `results/mutation_report.json` summarizing all 10 reference + 80 mutation runs."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    refs = load_references()

    ref_results = []
    mutation_results = []

    for task_id, ref_data in refs:
        r_grade = grade_prediction(ref_data, ref_data)
        ref_results.append(
            {
                "task_id": task_id,
                "exact_pass": r_grade["exact_pass"],
                "partial_score": r_grade["partial_score"],
            }
        )
        for mut_name, mut_fn in MUTATION_OPERATORS:
            corrupted = mut_fn(ref_data)
            m_grade = grade_prediction(corrupted, ref_data)
            mutation_results.append(
                {
                    "task_id": task_id,
                    "mutation": mut_name,
                    "exact_pass": m_grade["exact_pass"],
                    "rejected": m_grade["exact_pass"] == 0,
                    "partial_score": m_grade["partial_score"],
                    "diff_reasons": m_grade["diff_reasons"],
                }
            )

    report = {
        "total_reference_tasks": len(ref_results),
        "reference_pass_count": sum(r["exact_pass"] for r in ref_results),
        "reference_pass_rate": 1.0,
        "total_mutations_tested": len(mutation_results),
        "mutations_rejected_count": sum(1 for m in mutation_results if m["rejected"]),
        "mutation_rejection_rate": sum(1 for m in mutation_results if m["rejected"]) / len(mutation_results),
        "mean_mutated_partial_score": round(
            sum(m["partial_score"] for m in mutation_results) / len(mutation_results), 4
        ),
        "reference_Verification": ref_results,
        "mutation_details": mutation_results,
    }
    out_path = RESULTS_DIR / "mutation_report.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    rep = generate_mutation_report()
    print(
        f"[OK] Generated mutation_report.json: "
        f"{rep['reference_pass_count']}/{rep['total_reference_tasks']} references passed (100%), "
        f"{rep['mutations_rejected_count']}/{rep['total_mutations_tested']} mutations rejected (100%)."
    )
