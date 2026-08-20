import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROTOTYPE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PROTOTYPE_DIR.parents[1]
RUST_BINARY = PROTOTYPE_DIR / "target" / "release" / "ghp-rust-prototype"
PYTHON_PROTOTYPES = ("httpx", "curl_cffi", "pycurl", "niquests")


def run(command, env):
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    elapsed = time.perf_counter() - started
    return elapsed, json.loads(result.stdout)


def counts(payload):
    if "counts" in payload:
        return payload["counts"]
    return {
        "issues": len(payload["issues"]),
        "pull_requests": len(payload["pull_requests"]),
        "recent_comments": len(payload["recent_comments"]),
        "commits": len(payload["commits"]),
    }


def implementation_commands(repo, cutoff):
    implementations = [
        (
            "python",
            [
                sys.executable,
                "-m",
                "ghp.cli",
                "--repo",
                repo,
                "--since",
                cutoff,
                "--json",
            ],
        )
    ]
    implementations.extend(
        (
            name,
            [
                sys.executable,
                str(PROTOTYPE_DIR / f"{name}_prototype.py"),
                repo,
                cutoff,
            ],
        )
        for name in PYTHON_PROTOTYPES
    )
    implementations.append(("rust", [str(RUST_BINARY), repo, cutoff]))
    return implementations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default="getzola/zola")
    parser.add_argument("--hours", type=float, default=2)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    implementations = implementation_commands(args.repo, cutoff)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_DIR / "src")
    with tempfile.TemporaryDirectory() as state_dir:
        env["XDG_STATE_HOME"] = state_dir
        for _ in range(args.warmup):
            for _name, command in implementations:
                run(command, env)

        samples = {name: [] for name, _command in implementations}
        matched_rounds = 0
        latest_counts = {}
        for index in range(args.runs):
            shift = index % len(implementations)
            order = implementations[shift:] + implementations[:shift]
            round_counts = {}
            for name, command in order:
                elapsed, payload = run(command, env)
                round_counts[name] = counts(payload)
                samples[name].append(elapsed)
            latest_counts = round_counts
            if (
                len({tuple(sorted(value.items())) for value in round_counts.values()})
                == 1
            ):
                matched_rounds += 1

    for name, _command in implementations:
        values = samples[name]
        print(
            f"{name:9} median={statistics.median(values):.3f}s "
            f"min={min(values):.3f}s max={max(values):.3f}s"
        )
    python_median = statistics.median(samples["python"])
    speedups = " ".join(
        f"{name}_speedup={python_median / statistics.median(samples[name]):.2f}x"
        for name, _command in implementations
        if name != "python"
    )
    reported_counts = " ".join(
        f"{name}_counts={latest_counts[name]}" for name, _command in implementations
    )
    print(
        f"{speedups} "
        f"count_matches={matched_rounds}/{args.runs} "
        f"{reported_counts} cutoff={cutoff}"
    )


if __name__ == "__main__":
    main()
