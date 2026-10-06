"""Ollama model/prompt benchmark (W6.1) — benchmarks what actually ships.

Differences from the old script:
  * calls `editor.clean()` with `SYSTEM_PROMPT` from config, so the guards and
    the shipped prompt are exercised (the old script built its own prompt copy
    with `.replace`, silently benchmarking something else);
  * cases live in `bench_cases.json` (including the regression set);
  * scoring uses whole-word matching, case-sensitive proper nouns and
    all-of semantics (the old `any()` + case-insensitive checks let an
    all-lowercase answer with dropped names pass);
  * each case runs N times; results (per-case table, pass rate, latency) are
    written to `bench_results/` (gitignored).

    python bench_editor.py                 # all installed models
    python bench_editor.py llama3.2        # one model
    python bench_editor.py --runs 5        # more samples per case
"""
import argparse
import json
import os
import re
import statistics
import sys
import time

_BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)

import editor  # noqa: E402
from config import BENCH_DIR, DEFAULT_SETTINGS, OLLAMA_TAGS_URL  # noqa: E402

CASES_PATH = os.path.join(_BASE, "bench_cases.json")
RUNS_DEFAULT = 3

LONG_CASE = {
    "name": "long_300_words",
    "input": " ".join(["The quarterly report shows steady growth across every region."] * 40),
    "must_include_ci": ["quarterly report"],
}


# ------------------------------------------------------------------ scoring
def norm(text):
    """Normalise numbers/ordinals/times + punctuation for comparison."""
    return " ".join(editor._norm_tokens(text or ""))


def word_present(text, phrase):
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text or "") is not None


def edit_distance(a, b):
    a, b = a.split(), b.split()
    prev = list(range(len(b) + 1))
    for i, wa in enumerate(a, 1):
        cur = [i]
        for j, wb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (wa != wb)))
        prev = cur
    return prev[-1]


def score_case(case, out, status):
    """Returns (passed: bool, notes: list[str])."""
    notes = []
    ok = True

    expect_guard = case.get("expect_guard")
    if expect_guard == "raw":
        if status == "cleaned":
            ok = False
            notes.append("guard let a bad rewrite through")
    elif expect_guard == "cleaned":
        if status != "cleaned":
            ok = False
            notes.append(f"guard rejected a good rewrite ({status})")

    for phrase in case.get("must_include", []):
        if not word_present(out, phrase):          # case-sensitive (proper nouns)
            ok = False
            notes.append(f"missing {phrase!r}")

    for phrase in case.get("must_include_ci", []):
        if phrase.lower() not in (out or "").lower():
            ok = False
            notes.append(f"missing {phrase!r}")

    for phrase in case.get("must_include_norm", []):
        if phrase.lower() not in norm(out):
            ok = False
            notes.append(f"missing normalised {phrase!r}")

    for phrase in case.get("must_not_include", []):
        if phrase in (out or ""):
            ok = False
            notes.append(f"contains {phrase!r}")

    for word in case.get("must_not_include_words", []):
        if word_present(out, word):
            ok = False
            notes.append(f"contains word {word!r}")

    expect_exact = case.get("expect_exact")
    if expect_exact is not None and norm(expect_exact) != norm(out):
        ok = False
        notes.append(f"exact mismatch (d={edit_distance(norm(expect_exact), norm(out))})")

    return ok, notes


# ---------------------------------------------------------------- running
def installed_models():
    import requests
    try:
        resp = requests.get(OLLAMA_TAGS_URL, timeout=5)
        return [m["name"] for m in resp.json().get("models", [])]
    except Exception as e:
        print(f"Ollama not reachable ({e}) — nothing to benchmark.")
        return []


def load_cases():
    with open(CASES_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)
    cases.append(LONG_CASE)
    return cases


def run_model(model, cases, runs, timeout_s):
    rows = []
    for case in cases:
        passed = 0
        latencies = []
        last = ("", "")
        for _ in range(runs):
            t0 = time.monotonic()
            out, status = editor.clean(
                case["input"], model=model, mode="always",
                timeout_s=timeout_s, keep_alive="30m")
            latencies.append(time.monotonic() - t0)
            ok, notes = score_case(case, out, status)
            passed += 1 if ok else 0
            last = (out, "; ".join(notes) or status)
        rows.append({
            "case": case["name"],
            "pass_rate": f"{passed}/{runs}",
            "passed": passed == runs,
            "median_s": round(statistics.median(latencies), 2),
            "notes": last[1],
            "sample": last[0][:90],
        })
    total = sum(1 for r in rows if r["passed"])
    return rows, total


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("models", nargs="*", help="models to test (default: all installed)")
    ap.add_argument("--runs", type=int, default=RUNS_DEFAULT)
    ap.add_argument("--timeout", type=float,
                    default=DEFAULT_SETTINGS["ollama_timeout_s"])
    args = ap.parse_args()

    cases = load_cases()
    models = args.models or installed_models()
    if not models:
        return 1

    results = {}
    for model in models:
        print(f"\n=== {model} ({len(cases)} cases × {args.runs} runs) ===")
        rows, total = run_model(model, cases, args.runs, args.timeout)
        results[model] = {"cases": rows, "fully_passed": total, "of": len(rows)}
        for row in rows:
            mark = "PASS" if row["passed"] else "FAIL"
            print(f"  [{mark}] {row['case']:<42} {row['pass_rate']:>5}  "
                  f"{row['median_s']:>5}s  {row['notes']}")
        print(f"  -> {total}/{len(rows)} cases fully passed")

    os.makedirs(BENCH_DIR, exist_ok=True)
    out_path = os.path.join(BENCH_DIR,
                            f"editor_{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"cases": [c["name"] for c in cases], "results": results}, f,
                  indent=2)
    print(f"\nwritten: {os.path.relpath(out_path, _BASE)}")
    print("Best:", max(results, key=lambda m: results[m]["fully_passed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
