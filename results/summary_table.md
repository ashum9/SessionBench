# SessionBench Evaluation Summary Table (240 Trials across 4 Models)

| Model | Condition | L1 Pass (k=2) | L2 Pass (k=3) | L3 Pass (k=4..5) | Mean Pass (`pass@1`) | `pass@k` | `pass^k` (Reliable) | Partial Credit | Avg Latency | Cost / Run |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `gemini-3.5-flash-lite` | Condition A (`no_tools`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 38.4% | 3.66s | $0.00 |
| `gemini-3.5-flash-lite` | Condition B (`with_python_tool`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 5.6% | 15.46s | $0.02 |
| `gemini-3.1-flash-lite-preview` | Condition A (`no_tools`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 37.9% | 5.13s | $0.01 |
| `gemini-3.1-flash-lite-preview` | Condition B (`with_python_tool`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 18.1% | 16.04s | $0.02 |
| `gemini-3.1-flash-lite` | Condition A (`no_tools`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 37.8% | 6.11s | $0.01 |
| `gemini-3.1-flash-lite` | Condition B (`with_python_tool`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 8.3% | 34.07s | $0.02 |
| `gemini-flash-lite-latest` | Condition A (`no_tools`) | 0.0% | 0.0% | 0.0% | **0.0%** | 0.0% | **0.0%** | 37.3% | 4.09s | $0.00 |
| `gemini-flash-lite-latest` | Condition B (`with_python_tool`) | 16.7% | 0.0% | 0.0% | **3.3%** | 10.0% | **0.0%** | 12.0% | 16.35s | $0.02 |
