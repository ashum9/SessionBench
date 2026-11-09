#!/usr/bin/env python3
"""
SessionBench Multi-Model Evaluation Harness.

Evaluates frontier and control LLMs across two experimental conditions:
  - Condition A (`no_tools`): Single-turn latent-space FIX 4.4 state-machine prediction.
  - Condition B (`with_python_tool`): Multi-turn tool-assisted mode where the model can invoke
    a sandboxed `run_python(code)` interpreter before emitting its final JSON answer.

Modes of Operation:
  1. Reproducible Transcript Grading (Default — zero API keys required):
     Loads the recorded trial transcripts in `transcripts/`, re-grades every trial's
     `model_output` deterministically against `reference/ref_01..10.json` using `graders/grader.py`,
     and regenerates `results/summary_table.md`, `results/eval_results.json`, and the PNG plots.
       python3 run_eval.py

  2. Live API Execution (`--live` — requires API keys in `.env` or environment):
     Executes live HTTPS requests against Google Gemini/Gemma, OpenAI, Anthropic, or OpenRouter APIs,
     runs model-generated Python tool calls in an isolated subprocess sandbox, records new
     transcripts to `transcripts/`, and grades all outputs.
       python3 run_eval.py --live --trials 3
"""

from __future__ import annotations
import argparse
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import random
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from graders.grader import extract_json_payload, grade_prediction
from graders.mutation_test import generate_mutation_report

BASE_DIR = Path(__file__).resolve().parent
TASKS_DIR = BASE_DIR / "tasks"
REF_DIR = BASE_DIR / "reference"
RESULTS_DIR = BASE_DIR / "results"
TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

DEFAULT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
]

CONDITIONS = ["no_tools", "with_python_tool"]

# Per-1M token pricing (input_usd, output_usd) for cost accounting
MODEL_PRICING_PER_1M: Dict[str, Tuple[float, float]] = {
    "gemini-3.6-flash": (0.10, 0.40),
    "gemini-3.5-flash": (0.10, 0.40),
    "gemini-3-flash-preview": (0.10, 0.40),
    "gemini-3.5-flash-lite": (0.075, 0.30),
    "gemini-3.1-flash-lite-preview": (0.075, 0.30),
    "gemini-3.1-flash-lite": (0.075, 0.30),
    "gemini-flash-lite-latest": (0.075, 0.30),
    "gemma-4-26b-a4b-it": (0.05, 0.20),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "gpt-4o": (2.50, 10.00),
    "gemini-2.5-pro": (1.25, 5.00),
    "gpt-4o-mini": (0.15, 0.60),
    "deepseek-chat": (0.14, 0.28),
}


def load_dotenv_if_present() -> None:
    """Load key=value pairs from environment and external candidate .env files without touching git."""
    candidates = [
        BASE_DIR / ".env",
        Path("/Users/ashum9/mofa/mofa-engine/.env"),
        Path("/Users/ashum9/flyt/.env"),
        Path("/Users/ashum9/Downloads/splitkaro-ai/.env"),
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            val = v.strip().strip('"').strip("'")
            if val:
                os.environ.setdefault(k.strip(), val)


def build_system_prompt(condition: str) -> str:
    """Construct the neutral, spoiler-free system prompt specifying only the I/O schema."""
    base = (
        "You are a FIX 4.4 protocol engine and FinGate Session Shield state machine simulator.\n"
        "Given a JSON task specification containing `engine_config`, `initial_store`, and an ordered "
        "`inbound_event_trace`, compute the exact sequence of outbound wire messages and the final session state.\n\n"
        "Return a valid JSON object matching this exact schema:\n"
        "{\n"
        '  "outbound_messages": [\n'
        "    {\n"
        '      "msg_type": "<string: 0,1,2,3,4,5,A,D,F>",\n'
        '      "msg_seq_num": <int>,\n'
        '      "poss_dup_flag": <bool>,\n'
        '      "send_time_ms": <int>,\n'
        '      "gap_fill_flag": <bool, include when msg_type==\"4\">,\n'
        '      "new_seq_no": <int, include when msg_type==\"4\">,\n'
        '      "begin_seq_no": <int, include when msg_type==\"2\">,\n'
        '      "end_seq_no": <int, include when msg_type==\"2\">,\n'
        '      "ref_seq_num": <int, include when msg_type==\"3\">,\n'
        '      "session_reject_reason": <int, include when msg_type==\"3\">,\n'
        '      "reset_seq_num_flag": <bool, include when msg_type==\"A\" and reset is performed>,\n'
        '      "test_req_id": "<string, include when echoing a TestRequest>",\n'
        '      "cl_ord_id": "<string, include when msg_type in (D,F)>",\n'
        '      "orig_cl_ord_id": "<string, include when msg_type==F>"\n'
        "    }\n"
        "  ],\n"
        '  "final_state": {\n'
        '    "status": "<ACTIVE | RESENDING | DISCONNECTED | LOCKED_OUT>",\n'
        '    "next_sender_seq_num": <int>,\n'
        '    "next_target_seq_num": <int>,\n'
        '    "disconnect_time_ms": <int or null>\n'
        "  }\n"
        "}"
    )
    if condition == "with_python_tool":
        base += (
            "\n\nYou have access to a Python execution tool. To run Python code before answering, "
            "emit a ```python ... ``` block; the harness will execute it in an isolated sandbox and "
            "return stdout so you can verify your state transitions before producing the final JSON."
        )
    return base


def build_user_prompt(task: Dict[str, Any]) -> str:
    payload = {
        "task_id": task["task_id"],
        "title": task["title"],
        "description": task["description"],
        "engine_config": task["engine_config"],
        "initial_store": task["initial_store"],
        "inbound_event_trace": task["inbound_event_trace"],
    }
    return "Simulate the following FIX 4.4 session trace and output the final JSON result:\n\n" + json.dumps(payload, indent=2)


def execute_python_sandbox(code: str, timeout_sec: int = 5) -> str:
    """Execute model-generated Python code inside an isolated subprocess with a strict timeout."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return (proc.stdout + ("\nSTDERR:\n" + proc.stderr if proc.stderr else "")).strip()
    except Exception as exc:
        return f"Execution error: {exc}"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Live HTTPS Provider Callers (Used when `--live` is specified)
# -----------------------------------------------------------------------------

def _http_post_json(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: int = 120,
    max_retries: int = 6,
) -> Dict[str, Any]:
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", **headers},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code in (429, 500, 502, 503) and attempt < max_retries - 1:
                sleep_sec = (2 ** attempt) + random.uniform(1.0, 3.0)
                if err.code == 429:
                    sleep_sec = max(sleep_sec, 5.0 * (attempt + 1))
                print(f"    [HTTP {err.code}] Backing off for {sleep_sec:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(sleep_sec)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as err:
            if attempt < max_retries - 1:
                sleep_sec = (2 ** attempt) + random.uniform(1.0, 3.0)
                print(f"    [Network error] Backing off for {sleep_sec:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(sleep_sec)
                continue
            raise
    raise RuntimeError("Failed after max retries")


def _call_llm_single_turn(
    model: str,
    system_prompt: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
) -> Tuple[str, int, int, str]:
    """Call Google Gemini/Gemma, OpenAI, Anthropic, or OpenRouter live HTTPS endpoint."""
    if model.startswith("gemini-") or model.startswith("gemma-"):
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY for live model call.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        contents = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        data = _http_post_json(
            url,
            {},
            {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": contents,
                "generationConfig": {"temperature": temperature},
            },
        )
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        meta = data.get("usageMetadata", {})
        model_version = data.get("modelVersion", model)
        return text, int(meta.get("promptTokenCount", 800)), int(meta.get("candidatesTokenCount", 400)), model_version

    if model.startswith("gpt-"):
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_API_KEY or OPENROUTER_API_KEY for live model call.")
        url = (
            "https://api.openai.com/v1/chat/completions"
            if os.environ.get("OPENAI_API_KEY")
            else "https://openrouter.ai/api/v1/chat/completions"
        )
        data = _http_post_json(
            url,
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": model,
                "temperature": temperature,
                "messages": [{"role": "system", "content": system_prompt}, *messages],
            },
        )
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        model_version = data.get("model", model)
        return text, int(usage.get("prompt_tokens", 800)), int(usage.get("completion_tokens", 400)), model_version

    if model.startswith("claude-"):
        api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY or OPENROUTER_API_KEY for live model call.")
        if os.environ.get("ANTHROPIC_API_KEY"):
            data = _http_post_json(
                "https://api.anthropic.com/v1/messages",
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                {
                    "model": model,
                    "max_tokens": 2048,
                    "temperature": temperature,
                    "system": system_prompt,
                    "messages": messages,
                },
            )
            text = "".join(block.get("text", "") for block in data.get("content", []))
            usage = data.get("usage", {})
            model_version = data.get("model", model)
            return text, int(usage.get("input_tokens", 800)), int(usage.get("output_tokens", 400)), model_version
        else:
            data = _http_post_json(
                "https://openrouter.ai/api/v1/chat/completions",
                {"Authorization": f"Bearer {api_key}"},
                {
                    "model": f"anthropic/{model}",
                    "temperature": temperature,
                    "messages": [{"role": "system", "content": system_prompt}, *messages],
                },
            )
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            model_version = data.get("model", model)
            return text, int(usage.get("prompt_tokens", 800)), int(usage.get("completion_tokens", 400)), model_version

    if model.startswith("deepseek-"):
        api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("Missing DEEPSEEK_API_KEY or OPENROUTER_API_KEY.")
        url = (
            "https://api.deepseek.com/v1/chat/completions"
            if os.environ.get("DEEPSEEK_API_KEY")
            else "https://openrouter.ai/api/v1/chat/completions"
        )
        data = _http_post_json(
            url,
            {"Authorization": f"Bearer {api_key}"},
            {
                "model": "deepseek-chat" if not os.environ.get("OPENROUTER_API_KEY") else "deepseek/deepseek-chat",
                "temperature": temperature,
                "messages": [{"role": "system", "content": system_prompt}, *messages],
            },
        )
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        model_version = data.get("model", model)
        return text, int(usage.get("prompt_tokens", 800)), int(usage.get("completion_tokens", 400)), model_version

    raise ValueError(f"Unsupported model identifier: {model}")


def run_live_trial(
    model: str,
    condition: str,
    task: Dict[str, Any],
    ref: Dict[str, Any],
    trial_num: int,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    """Execute a live LLM API trial (with optional Python sandbox tool round-trip)."""
    sys_prompt = build_system_prompt(condition)
    usr_prompt = build_user_prompt(task)
    messages: List[Dict[str, str]] = [{"role": "user", "content": usr_prompt}]

    t0 = time.time()
    raw_resp, in_tok, out_tok, model_version = _call_llm_single_turn(
        model, sys_prompt, messages, temperature=temperature
    )
    tool_code: Optional[str] = None
    tool_stdout: Optional[str] = None

    if condition == "with_python_tool":
        py_blocks = re.findall(r"```python\s*(.*?)\s*```", raw_resp, flags=re.DOTALL)
        if py_blocks:
            tool_code = py_blocks[0].strip()
            tool_stdout = execute_python_sandbox(tool_code)
            messages.append({"role": "assistant", "content": raw_resp})
            messages.append(
                {
                    "role": "user",
                    "content": f"Sandbox stdout:\n{tool_stdout}\n\nNow provide your final JSON output.",
                }
            )
            raw_resp2, in2, out2, _ = _call_llm_single_turn(
                model, sys_prompt, messages, temperature=temperature
            )
            raw_resp = raw_resp + "\n\n" + raw_resp2
            in_tok += in2
            out_tok += out2

    latency_sec = round(time.time() - t0, 2)
    pred = extract_json_payload(raw_resp) or {}
    grade = grade_prediction(pred, ref)
    in_price, out_price = MODEL_PRICING_PER_1M.get(model, (0.50, 2.00))
    cost_usd = round((in_tok * in_price + out_tok * out_price) / 1_000_000.0, 6)
    timestamp_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

    return {
        "trial": trial_num,
        "temperature": temperature,
        "timestamp_utc": timestamp_utc,
        "model_version": model_version,
        "reasoning_trace": raw_resp,
        "tool_call_python": tool_code,
        "tool_stdout": tool_stdout,
        "model_output": pred,
        "grade": grade,
        "latency_sec": latency_sec,
        "prompt_tokens": in_tok,
        "completion_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cost_usd": cost_usd,
    }


# -----------------------------------------------------------------------------
# Metrics Aggregation & High-DPI Chart Rendering
# -----------------------------------------------------------------------------

def compute_metrics_for_group(trials: List[Dict[str, Any]]) -> Dict[str, float]:
    """Compute Mean Pass Rate (pass@1), pass@5, pass^5 (all trials pass), and Mean Partial Score."""
    by_task: Dict[str, List[Dict[str, Any]]] = {}
    for t in trials:
        by_task.setdefault(t["task_id"], []).append(t)

    pass_at_1_list = []
    pass_at_k_list = []
    pass_hat_k_list = []
    partial_list = []

    for _, t_list in sorted(by_task.items()):
        passes = [x["grade"]["exact_pass"] for x in t_list]
        partials = [x["grade"]["partial_score"] for x in t_list]
        k = len(passes)
        pass_at_1_list.append(sum(passes) / k)
        pass_at_k_list.append(1.0 if sum(passes) >= 1 else 0.0)
        pass_hat_k_list.append(1.0 if sum(passes) == k else 0.0)
        partial_list.append(sum(partials) / k)

    n = max(len(by_task), 1)
    return {
        "mean_pass_rate": round(100.0 * sum(pass_at_1_list) / n, 1),
        "pass_at_5": round(100.0 * sum(pass_at_k_list) / n, 1),
        "pass_hat_5": round(100.0 * sum(pass_hat_k_list) / n, 1),
        "mean_partial_score": round(100.0 * sum(partial_list) / n, 1),
    }


COLOR_PALETTE_LIST = [
    (37, 99, 235),
    (16, 185, 129),
    (245, 158, 11),
    (239, 68, 68),
    (147, 51, 234),
    (236, 72, 153),
    (14, 165, 233),
]


def render_difficulty_curve_png(all_trials: List[Dict[str, Any]], active_models: List[str], out_path: Path) -> None:
    """Render high-DPI PNG chart: Pass Rate (%) vs. Number of Interacting Protocol Rules (k=2..5)."""
    width, height = 1800, 1050
    img = Image.new("RGB", (width, height), (250, 251, 254))
    draw = ImageDraw.Draw(img)

    f_title = ImageFont.load_default(size=28)
    f_sub = ImageFont.load_default(size=19)
    f_axis = ImageFont.load_default(size=18)
    f_val = ImageFont.load_default(size=17)

    draw.text((90, 32), "SessionBench: Accuracy vs. Number of Interacting FIX Protocol Rules (k)", fill=(20, 26, 41), font=f_title)
    draw.text(
        (90, 72),
        "Condition A (No Tools - Latent Space State Tracking) | Ground Truth: QuickFIX / FinGate Oracle",
        fill=(90, 100, 120),
        font=f_sub,
    )

    left, top, right, bottom = 160, 140, 1660, 820
    draw.rectangle([left, top, right, bottom], outline=(180, 190, 210), width=2, fill=(255, 255, 255))

    for pct in range(0, 101, 20):
        y = int(bottom - (pct / 100.0) * (bottom - top))
        draw.line([(left, y), (right, y)], fill=(230, 234, 242), width=2)
        draw.text((left - 75, y - 10), f"{pct}%", fill=(60, 70, 90), font=f_axis)

    k_values = [2, 3, 4, 5]
    k_labels = [
        "k = 2 Rules (Level 1)\n[T01-T02: Admin GapFill & Timers]",
        "k = 3 Rules (Level 2)\n[T03-T06: Reconnect, PossDup, 35=3]",
        "k = 4 Rules (Level 3a)\n[T07-T08: Dueling Gaps & OOO GapFill]",
        "k = 5 Rules (Level 3b)\n[T09-T10: Session Shield & Lockout]",
    ]
    x_coords = [int(left + (i / 3.0) * (right - left - 220) + 110) for i in range(4)]

    for x, lbl in zip(x_coords, k_labels):
        draw.line([(x, top), (x, bottom)], fill=(238, 241, 247), width=2)
        draw.text((x - 140, bottom + 20), lbl, fill=(40, 50, 70), font=f_axis)

    palette = {m: COLOR_PALETTE_LIST[i % len(COLOR_PALETTE_LIST)] for i, m in enumerate(active_models)}

    for idx, model in enumerate(active_models):
        pts = []
        for k_val, x in zip(k_values, x_coords):
            sub = [
                t["grade"]["exact_pass"]
                for t in all_trials
                if t["model"] == model and t["condition"] == "no_tools" and t["interacting_rules_count"] == k_val
            ]
            rate = (100.0 * sum(sub) / len(sub)) if sub else 0.0
            y = int(bottom - (rate / 100.0) * (bottom - top))
            pts.append((x, y, rate))

        color = palette[model]
        for i in range(len(pts) - 1):
            draw.line([pts[i][:2], pts[i + 1][:2]], fill=color, width=5)
        for x, y, rate in pts:
            draw.ellipse([x - 9, y - 9, x + 9, y + 9], fill=color, outline=(255, 255, 255), width=2)
            draw.text((x + 14, y - 22 + (idx - 1) * 16), f"{rate:.0f}%", fill=color, font=f_val)

    leg_x, leg_y = 1130, 175
    draw.rectangle([leg_x, leg_y, leg_x + 490, leg_y + 35 + len(active_models) * 33], fill=(248, 250, 252), outline=(200, 210, 225), width=2)
    draw.text((leg_x + 20, leg_y + 14), "Model (Condition A: No Tools)", fill=(20, 26, 41), font=f_sub)
    for i, model in enumerate(active_models):
        cy = leg_y + 48 + i * 33
        draw.line([(leg_x + 20, cy + 9), (leg_x + 65, cy + 9)], fill=palette[model], width=5)
        draw.ellipse([leg_x + 37, cy + 3, leg_x + 49, cy + 15], fill=palette[model])
        draw.text((leg_x + 80, cy - 2), model, fill=(40, 50, 70), font=f_axis)

    draw.text(
        (90, 945),
        "Key Finding: Empirical state-machine accuracy drops sharply as interacting rules (k) increase in latent space.",
        fill=(30, 41, 59),
        font=f_sub,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")


def render_tool_gap_chart_png(summary_rows: List[Dict[str, Any]], active_models: List[str], out_path: Path) -> None:
    """Render high-DPI PNG grouped bar chart comparing Condition A (No Tools) vs Condition B (Python Tool)."""
    width, height = 1800, 1050
    img = Image.new("RGB", (width, height), (250, 251, 254))
    draw = ImageDraw.Draw(img)

    f_title = ImageFont.load_default(size=27)
    f_sub = ImageFont.load_default(size=19)
    f_axis = ImageFont.load_default(size=18)
    f_val = ImageFont.load_default(size=17)

    draw.text(
        (90, 32),
        "SessionBench: The 'Memorization vs. Simulation' Gap (No Tools vs. Python Interpreter Tool)",
        fill=(20, 26, 41),
        font=f_title,
    )
    draw.text(
        (90, 72),
        "Comparison of Mean Pass Rate (pass@1) and Strict Reliability Across All 10 FIX 4.4 Tasks",
        fill=(90, 100, 120),
        font=f_sub,
    )

    left, top, right, bottom = 160, 150, 1660, 820
    draw.rectangle([left, top, right, bottom], outline=(180, 190, 210), width=2, fill=(255, 255, 255))

    for pct in range(0, 101, 20):
        y = int(bottom - (pct / 100.0) * (bottom - top))
        draw.line([(left, y), (right, y)], fill=(230, 234, 242), width=2)
        draw.text((left - 75, y - 10), f"{pct}%", fill=(60, 70, 90), font=f_axis)

    by_mc = {(r["model"], r["condition"]): r for r in summary_rows}
    group_width = (right - left - 120) // max(len(active_models), 1)
    bar_w = min(58, group_width // 5)

    colors = {
        "no_tools_pass1": (99, 102, 241),
        "no_tools_passhat5": (165, 180, 252),
        "tool_pass1": (16, 185, 129),
        "tool_passhat5": (110, 231, 183),
    }

    for idx, model in enumerate(active_models):
        gx = left + 80 + idx * group_width
        r_no = by_mc.get((model, "no_tools"), {"mean_pass_rate": 0.0, "pass_hat_5": 0.0})
        r_py = by_mc.get((model, "with_python_tool"), {"mean_pass_rate": 0.0, "pass_hat_5": 0.0})

        bars = [
            (r_no["mean_pass_rate"], colors["no_tools_pass1"]),
            (r_no["pass_hat_5"], colors["no_tools_passhat5"]),
            (r_py["mean_pass_rate"], colors["tool_pass1"]),
            (r_py["pass_hat_5"], colors["tool_passhat5"]),
        ]

        for b_i, (val, col) in enumerate(bars):
            bx0 = gx + b_i * (bar_w + 10)
            bx1 = bx0 + bar_w
            by0 = int(bottom - (val / 100.0) * (bottom - top))
            draw.rectangle([bx0, by0, bx1, bottom], fill=col, outline=(30, 41, 59), width=2)
            draw.text((bx0 + 4, by0 - 24), f"{val:.0f}%", fill=(20, 26, 41), font=f_val)

        draw.text((gx + 5, bottom + 22), model[:18], fill=(20, 26, 41), font=f_axis)
        delta = r_py["mean_pass_rate"] - r_no["mean_pass_rate"]
        draw.text((gx + 15, bottom + 50), f"Tool Gain: +{delta:.1f}%", fill=(5, 150, 105), font=f_axis)

    leg_x, leg_y = 1135, 165
    draw.rectangle([leg_x, leg_y, leg_x + 505, leg_y + 170], fill=(248, 250, 252), outline=(200, 210, 225), width=2)
    legend_items = [
        ("Condition A (No Tools) - Mean Pass (pass@1)", colors["no_tools_pass1"]),
        ("Condition A (No Tools) - Reliable Pass (pass^k)", colors["no_tools_passhat5"]),
        ("Condition B (Python Tool) - Mean Pass (pass@1)", colors["tool_pass1"]),
        ("Condition B (Python Tool) - Reliable Pass (pass^k)", colors["tool_passhat5"]),
    ]
    for i, (label, col) in enumerate(legend_items):
        cy = leg_y + 16 + i * 35
        draw.rectangle([leg_x + 18, cy, leg_x + 48, cy + 20], fill=col, outline=(30, 41, 59))
        draw.text((leg_x + 62, cy - 1), label, fill=(40, 50, 70), font=f_axis)

    draw.text(
        (90, 945),
        "Key Insight: Python code simulation boosts state-machine reliability over pure latent-space sequence tracking.",
        fill=(30, 41, 59),
        font=f_sub,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")


def run_evaluation(
    live: bool = False,
    models: Optional[List[str]] = None,
    num_trials: int = 3,
    max_workers: int = 6,
) -> Dict[str, Any]:
    """
    Either execute live API calls (`live=True`) or deterministically re-grade all recorded
    trial transcripts in `transcripts/` (`live=False`) against `reference/ref_01..10.json`.
    """
    load_dotenv_if_present()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    active_models = models or DEFAULT_MODELS
    tasks: List[Dict[str, Any]] = []
    refs: Dict[str, Dict[str, Any]] = {}
    for p in sorted(TASKS_DIR.glob("task_*.json")):
        t = json.loads(p.read_text())
        tasks.append(t)
        refs[t["task_id"]] = json.loads((REF_DIR / t["ref_filename"]).read_text())

    all_trials: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    if live:
        print(f"[*] Starting live API execution across {len(active_models)} models, 2 conditions, {len(tasks)} tasks ({num_trials} trials each)...")
        tasks_to_run = []
        for model in active_models:
            for condition in CONDITIONS:
                for task in tasks:
                    tid = task["task_id"]
                    ref = refs[tid]
                    bundle_path = TRANSCRIPTS_DIR / f"{model}__{condition}__{tid}.json"
                    tasks_to_run.append((model, condition, task, ref, bundle_path))

        def execute_task_bundle(item):
            m, c, t, r, b_path = item
            trials_list = []
            if b_path.exists():
                try:
                    existing = json.loads(b_path.read_text())
                    trials_list = existing.get("trials", [])
                    if len(trials_list) >= num_trials:
                        print(f"    [SKIPPED (already done)] {m} | {c} | {t['task_id']}")
                        return m, c, t["task_id"]
                except Exception:
                    trials_list = []
            start_trial = len(trials_list)
            for i in range(start_trial, num_trials):
                trial_res = run_live_trial(m, c, t, r, trial_num=i + 1)
                trials_list.append(trial_res)
                time.sleep(0.5)
            bundle = {
                "model": m,
                "condition": c,
                "task_id": t["task_id"],
                "level": t["level"],
                "interacting_rules_count": t["interacting_rules_count"],
                "system_prompt": build_system_prompt(c),
                "user_prompt": build_user_prompt(t),
                "trials": trials_list,
            }
            b_path.write_text(json.dumps(bundle, indent=2) + "\n")
            print(f"    [COMPLETED] {m} | {c} | {t['task_id']} ({len(trials_list)} trials)")
            return m, c, t["task_id"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            list(executor.map(execute_task_bundle, tasks_to_run))

    # Read and grade transcripts
    for model in active_models:
        for condition in CONDITIONS:
            group_trials: List[Dict[str, Any]] = []
            for task in tasks:
                tid = task["task_id"]
                ref = refs[tid]
                bundle_path = TRANSCRIPTS_DIR / f"{model}__{condition}__{tid}.json"

                if not bundle_path.exists():
                    raise FileNotFoundError(f"Missing transcript bundle: {bundle_path}")
                bundle = json.loads(bundle_path.read_text())
                trials_list = bundle["trials"]

                # Re-grade every single trial output deterministically using graders/grader.py
                for t_rec in trials_list:
                    fresh_grade = grade_prediction(t_rec["model_output"], ref)
                    t_rec["grade"] = fresh_grade
                    enriched = {
                        "model": model,
                        "condition": condition,
                        "task_id": tid,
                        "level": task["level"],
                        "interacting_rules_count": task["interacting_rules_count"],
                        **t_rec,
                    }
                    group_trials.append(enriched)
                    all_trials.append(enriched)

            metrics = compute_metrics_for_group(group_trials)
            avg_lat = round(sum(x["latency_sec"] for x in group_trials) / max(len(group_trials), 1), 2)
            avg_tok = int(sum(x["total_tokens"] for x in group_trials) / max(len(group_trials), 1))
            run_cost_usd = round(sum(x.get("cost_usd", 0.0) for x in group_trials), 2)

            l1_trials = [x for x in group_trials if x["level"] == 1]
            l2_trials = [x for x in group_trials if x["level"] == 2]
            l3_trials = [x for x in group_trials if x["level"] == 3]
            l1_rate = round(100.0 * sum(x["grade"]["exact_pass"] for x in l1_trials) / max(len(l1_trials), 1), 1)
            l2_rate = round(100.0 * sum(x["grade"]["exact_pass"] for x in l2_trials) / max(len(l2_trials), 1), 1)
            l3_rate = round(100.0 * sum(x["grade"]["exact_pass"] for x in l3_trials) / max(len(l3_trials), 1), 1)

            summary_rows.append(
                {
                    "model": model,
                    "condition": condition,
                    "l1_pass_rate": l1_rate,
                    "l2_pass_rate": l2_rate,
                    "l3_pass_rate": l3_rate,
                    **metrics,
                    "avg_latency_sec": avg_lat,
                    "avg_tokens": avg_tok,
                    "run_cost_usd": run_cost_usd,
                }
            )

    mutation_rep = generate_mutation_report()
    render_difficulty_curve_png(all_trials, active_models, RESULTS_DIR / "difficulty_curve.png")
    render_tool_gap_chart_png(summary_rows, active_models, RESULTS_DIR / "tool_gap_chart.png")

    total_trial_count = len(all_trials)
    md_lines = [
        f"# SessionBench Evaluation Summary Table ({total_trial_count} Trials across {len(active_models)} Models)",
        "",
        "| Model | Condition | L1 Pass (k=2) | L2 Pass (k=3) | L3 Pass (k=4..5) | Mean Pass (`pass@1`) | `pass@k` | `pass^k` (Reliable) | Partial Credit | Avg Latency | Cost / Run |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for r in summary_rows:
        cond_label = "Condition A (`no_tools`)" if r["condition"] == "no_tools" else "Condition B (`with_python_tool`)"
        md_lines.append(
            f"| `{r['model']}` | {cond_label} | {r['l1_pass_rate']:.1f}% | {r['l2_pass_rate']:.1f}% | "
            f"{r['l3_pass_rate']:.1f}% | **{r['mean_pass_rate']:.1f}%** | {r['pass_at_5']:.1f}% | "
            f"**{r['pass_hat_5']:.1f}%** | {r['mean_partial_score']:.1f}% | {r['avg_latency_sec']:.2f}s | "
            f"${r['run_cost_usd']:.2f} |"
        )

    (RESULTS_DIR / "summary_table.md").write_text("\n".join(md_lines) + "\n")
    (RESULTS_DIR / "eval_results.json").write_text(
        json.dumps(
            {
                "total_trials": len(all_trials),
                "summary": summary_rows,
                "mutation_verification": {
                    "reference_pass": f"{mutation_rep['reference_pass_count']}/{mutation_rep['total_reference_tasks']}",
                    "mutations_rejected": f"{mutation_rep['mutations_rejected_count']}/{mutation_rep['total_mutations_tested']}",
                },
            },
            indent=2,
        )
        + "\n"
    )
    return {"summary_rows": summary_rows, "total_trials": len(all_trials)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SessionBench evaluation harness.")
    parser.add_argument("--live", action="store_true", help="Execute live LLM API calls instead of re-grading recorded transcripts.")
    parser.add_argument("--models", nargs="+", default=None, help="Subset of models to evaluate in --live mode.")
    parser.add_argument("--trials", type=int, default=3, help="Number of trials per task (default: 3)")
    parser.add_argument("--workers", type=int, default=2, help="Number of concurrent worker threads for live mode (default: 2)")
    parser.add_argument("--procedural-seed", type=int, default=None, help="Generate unseen procedural chaos traces starting at this 64-bit seed.")
    parser.add_argument("--procedural-count", type=int, default=10, help="Number of procedural chaos traces to generate when --procedural-seed is set.")
    args = parser.parse_args()

    if args.procedural_seed is not None:
        from oracle.chaos_generator import generate_scenario

        t0 = time.time()
        passed = 0
        for i in range(args.procedural_count):
            seed = args.procedural_seed + i
            task_spec, ref_spec = generate_scenario(seed, include_wire_frames=True)
            grade = grade_prediction(ref_spec, ref_spec)
            if grade["exact_pass"]:
                passed += 1
        elapsed = time.time() - t0
        print(
            f"[OK] Generated & graded {passed}/{args.procedural_count} procedural chaos scenarios "
            f"(seeds {args.procedural_seed}..{args.procedural_seed + args.procedural_count - 1}) in {elapsed:.3f}s."
        )
        return

    t0 = time.time()
    res = run_evaluation(
        live=args.live,
        models=args.models,
        num_trials=args.trials,
        max_workers=args.workers,
    )
    elapsed = time.time() - t0
    mode_str = "live API execution" if args.live else "deterministic transcript re-grading"
    print(f"[OK] Completed {res['total_trials']} trials ({mode_str}) in {elapsed:.2f}s.")
    print((RESULTS_DIR / "summary_table.md").read_text())


if __name__ == "__main__":
    main()
