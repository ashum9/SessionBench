#!/usr/bin/env python3
"""
SessionBench Agentic Code-Repair Conformance Evaluator ("SWE-Bench for FIX Engines").

Evaluates a candidate FIX 4.4 engine implementation against:
  1. The 10 canonical SessionBench tasks (T01..T10)
  2. 100 procedurally generated multi-fault chaos traces (seeds 1000..1099)

Compares the unpatched `CandidateFIXEngine` (`code_repair/broken_engine.py`) against
the patched reference state machine (`FIX44SessionEngine` in `oracle/fix44_spec_rules.py`).

Usage:
    python3 code_repair/evaluate_repair.py --seed 1000 --count 100
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Type

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from code_repair.broken_engine import CandidateFIXEngine
from graders.grader import grade_prediction
from oracle.chaos_generator import generate_scenario
from oracle.fix44_spec_rules import FIX44SessionEngine, verify_fingate_invariants


def evaluate_engine_class(
    engine_cls: Type[Any],
    engine_name: str,
    seed_start: int = 1000,
    chaos_count: int = 100,
) -> Dict[str, Any]:
    """Run an engine class against all 10 canonical tasks + `chaos_count` procedural seeds."""
    tasks_dir = ROOT_DIR / "tasks"
    refs_dir = ROOT_DIR / "reference"

    canonical_passed = 0
    canonical_partial: List[float] = []
    invariant_violation_count = 0

    for t_path in sorted(tasks_dir.glob("task_*.json")):
        task_spec = json.loads(t_path.read_text(encoding="utf-8"))
        ref_spec = json.loads((refs_dir / task_spec["ref_filename"]).read_text(encoding="utf-8"))

        eng = engine_cls(task_spec)
        pred = eng.run_trace(task_spec["inbound_event_trace"])
        grade = grade_prediction(pred, ref_spec)
        violations = verify_fingate_invariants(task_spec, pred)

        if grade["exact_pass"]:
            canonical_passed += 1
        canonical_partial.append(float(grade["partial_score"]))
        invariant_violation_count += len(violations)

    chaos_passed = 0
    chaos_partial: List[float] = []
    for i in range(chaos_count):
        seed = seed_start + i
        task_spec, ref_spec = generate_scenario(seed, include_wire_frames=True)
        eng = engine_cls(task_spec)
        pred = eng.run_trace(task_spec["inbound_event_trace"])
        grade = grade_prediction(pred, ref_spec)
        violations = verify_fingate_invariants(task_spec, pred)

        if grade["exact_pass"]:
            chaos_passed += 1
        chaos_partial.append(float(grade["partial_score"]))
        invariant_violation_count += len(violations)

    total_scenarios = 10 + chaos_count
    total_passed = canonical_passed + chaos_passed
    all_partials = canonical_partial + chaos_partial

    return {
        "engine_name": engine_name,
        "canonical_10_pass_rate": round(100.0 * canonical_passed / 10.0, 1),
        "canonical_10_passed": f"{canonical_passed}/10",
        "procedural_chaos_pass_rate": round(100.0 * chaos_passed / max(1, chaos_count), 1),
        "procedural_chaos_passed": f"{chaos_passed}/{chaos_count}",
        "overall_pass_rate": round(100.0 * total_passed / total_scenarios, 1),
        "mean_partial_score": round(100.0 * sum(all_partials) / len(all_partials), 1),
        "fingate_invariant_violations": invariant_violation_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate FIX 4.4 Code-Repair Conformance")
    parser.add_argument("--seed", type=int, default=1000, help="Starting seed for procedural chaos suite")
    parser.add_argument("--count", type=int, default=100, help="Number of procedural chaos traces")
    args = parser.parse_args()

    t0 = time.time()
    broken_res = evaluate_engine_class(
        CandidateFIXEngine,
        "Unpatched CandidateFIXEngine (code_repair/broken_engine.py)",
        seed_start=args.seed,
        chaos_count=args.count,
    )
    patched_res = evaluate_engine_class(
        FIX44SessionEngine,
        "Patched Reference Engine (oracle/fix44_spec_rules.py)",
        seed_start=args.seed,
        chaos_count=args.count,
    )
    elapsed = time.time() - t0

    report = {
        "seed_range": f"{args.seed}..{args.seed + args.count - 1}",
        "total_scenarios_per_engine": 10 + args.count,
        "elapsed_sec": round(elapsed, 3),
        "results": [broken_res, patched_res],
    }

    out_path = ROOT_DIR / "results" / "code_repair_benchmark.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"[OK] Evaluated {10 + args.count} scenarios per engine in {elapsed:.3f}s:")
    print(
        "| Engine Implementation | Canonical Tasks (`10`) | Procedural Chaos (`100` Seeds) | Overall Conformance | Partial Score | FinGate Invariant Violations |"
    )
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for r in [broken_res, patched_res]:
        print(
            f"| {r['engine_name']} | {r['canonical_10_passed']} ({r['canonical_10_pass_rate']:.1f}%) | "
            f"{r['procedural_chaos_passed']} ({r['procedural_chaos_pass_rate']:.1f}%) | "
            f"**{r['overall_pass_rate']:.1f}%** | {r['mean_partial_score']:.1f}% | "
            f"`{r['fingate_invariant_violations']}` |"
        )


if __name__ == "__main__":
    main()
