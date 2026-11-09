# SessionBench — 4-Minute Video Walkthrough Script (micro1 Eval Hackathon)

> **Target Duration:** 3 minutes 50 seconds – 4 minutes 15 seconds  
> **Screen Layout:** Keep terminal + IDE open to `/Users/ashum9/projects/FinGate/sessionbench` and preview `README.md`, `results/difficulty_curve.png`, and `results/tool_gap_chart.png`.

---

## [0:00 – 0:50] Part 1: The Problem & Real-World Financial Stakes
* **On Screen:** Show `README.md` Section 1 & FinGate architecture diagram.
* **What to Say:**
  > "Hi everyone, this is **SessionBench**—a deterministic benchmark evaluating whether frontier LLMs can simulate and repair stateful FIX 4.4 order-routing and session-recovery state machines.
  >
  > Global capital markets route trillions of dollars every day over the FIX protocol. Unlike stateless REST APIs, a FIX session is a strict, persistent sequence-numbered state machine. While building **FinGate**—our Rust FIX session broker and order gateway—we noticed a striking paradox: frontier LLMs can recite FIX tag specifications from memory with 99% accuracy, yet when asked to trace or implement crash recovery, bidirectional sequence gaps, or pod disconnect handling, they silently hallucinate sequence numbers and corrupt the session state."

---

## [0:50 – 2:00] Part 2: The 10-Task Ladder, Procedural Chaos Generator & 80-Mutation Grader Proof
* **On Screen:**
  1. Show `oracle/fix44_spec_rules.py` and `tasks/task_08_out_of_order_gapfill.json`.
  2. Run `python3 oracle/generate_reference.py --verify`, `pytest -v graders/mutation_test.py` (`90 passed in 0.04s`), and `python3 oracle/chaos_generator.py --seed 1000 --count 100 --verify`.
* **What to Say:**
  > "To measure this rigorously with zero subjective LLM-as-a-judge noise, we built an executable FIX 4.4 + FinGate Session Shield reference oracle (`oracle/fix44_spec_rules.py`) encoding 25 formal protocol rules—including wire-level SOH framing, `BodyLength (9)`, and `CheckSum (10)` validation (`R25`)—and enforcing all 6 FinGate session invariants (`I1`–`I6`).
  >
  > We designed a **10-task canonical difficulty ladder** parameterized by the number of simultaneously interacting rules $k$ (from $k=2$ at Level 1 up to $k=5$ at Level 3), plus a **procedural seeded chaos generator** (`oracle/chaos_generator.py`) that synthesizes unlimited contamination-proof multi-fault FIX scenarios on the fly.
  >
  > Our deterministic grader (`graders/grader.py`) computes both strict binary `exact_pass` and a 4-component `partial_score`. To prove the grader cannot be tricked, `graders/mutation_test.py` injects 8 domain-specific protocol faults across all 10 tasks—producing **80 mutated outputs** such as off-by-one `NewSeqNo`, uncoalesced admin gap-fills, and missing `PossDupFlag`s. In `pytest`, **10 out of 10 reference solutions pass 100%, and 80 out of 80 mutated outputs are 100% rejected**."

---

## [2:00 – 3:10] Part 3: Failure Transcripts & The "Close Call" Analysis
* **On Screen:** Open `transcripts/gpt-4o__no_tools__T08.json`, `transcripts/gpt-4o__no_tools__T06.json`, and `results/difficulty_curve.png`.
* **What to Say:**
  > "Looking at `results/difficulty_curve.png`, when $k=2$ rules interact, `claude-3-5-sonnet`, `gpt-4o`, and `gemini-2.5-pro` achieve 90 to 100% accuracy. But as soon as $k \ge 4$ rules interact in latent space, accuracy collapses to 10–30%.
  >
  > Why? Look at `T08` (`transcripts/gpt-4o__no_tools__T08.json`): the engine expects sequence `10`, and an out-of-order `SequenceReset-GapFill` arrives at sequence `12` with `NewSeqNo=16`. In latent space, models confuse `GapFill (123=Y)` with hard `Reset (123=N)` and jump `NextTargetMsgSeqNum` straight to `16`—silently dropping live orders `10` and `11`!
  >
  > Similarly, in `T06`, models score 73% partial credit—getting `EndSeqNo=0` right—but misclassify `SessionReject (35=3)` as an application message and resend it verbatim instead of coalescing it into a `GapFill`, which immediately drops a real QuickFIX session."

---

## [3:10 – 4:05] Part 4: Two-Condition Tool Gap, Agentic Code Repair & Native Rust Verifier
* **On Screen:** Show `results/tool_gap_chart.png`, run `python3 run_eval.py`, run `python3 code_repair/evaluate_repair.py`, and run `cargo run --offline --release --bin fingate_conformance` from `/Users/ashum9/projects/FinGate`.
* **What to Say:**
  > "Next, we ran a two-condition experiment across 400 trials comparing **Condition A (`no_tools`)** against **Condition B (`with_python_tool`)**, where the model can execute a Python state-machine script before answering.
  >
  > As `results/tool_gap_chart.png` shows, granting a Python interpreter boosts frontier pass rates by **+34% to +40%** (`claude-3-5-sonnet` jumps from 58% to 92%, and its 5-out-of-5 `pass^5` reliability doubles from 30% to 60%).
  >
  > Finally, we extended SessionBench with an **Agentic Code-Repair Track** (`code_repair/evaluate_repair.py`) testing broken engine implementations across 110 scenarios (`3.6%` unpatched conformance vs. `100.0%` patched), and a **native Rust `SimEnv` conformance verifier** (`fingate_conformance`) that validates all 10 reference traces and wire frames in `4.2 milliseconds`.
  >
  > This proves our core takeaway: frontier models *know* the individual protocol rules, but fail at multi-step latent state tracking under non-local rule interactions—making external symbolic execution and deterministic protocol conformance gates essential for financial infrastructure."
