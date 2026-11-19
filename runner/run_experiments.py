import os
import sys
import time
import socket
import subprocess
from typing import Dict, Any, List, Optional
import pandas as pd
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text

# Add runner, root, and booking-api to path
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "booking-api")))

from client import RetryClient


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://retrysafe:retrysafe@127.0.0.1:5432/retrysafe"
)
db_engine = create_engine(DATABASE_URL, pool_pre_ping=True)

RESULTS_DIR = "results"
SEEDS = [42, 101, 2024]
SCENARIOS = [
    ("F1_drop_request", "drop_request", "Request Lost", None),
    ("F2_drop_response", "drop_response", "Response Lost", None),
    ("F3_server_crash", "crash", "Server Crash", None),
    ("F4_duplicate", "duplicate", "Duplicate Delivery", {"X-Duplicate-Mode": "delay", "X-Duplicate-Delay": "50"}),
]


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


def wait_for_port(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_port_open(port, host):
            return True
        time.sleep(0.2)
    return False


def wait_for_port_closed(port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not is_port_open(port, host):
            return True
        time.sleep(0.2)
    return False


def start_api_process(idempotency: bool) -> subprocess.Popen:
    """Start Booking API with explicit IDEMPOTENCY env flag."""
    env = os.environ.copy()
    env["IDEMPOTENCY"] = "on" if idempotency else "off"
    env["DATABASE_URL"] = DATABASE_URL

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd="booking-api",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if not wait_for_port(8000):
        proc.terminate()
        raise RuntimeError(f"Booking API failed to start on port 8000 with IDEMPOTENCY={env['IDEMPOTENCY']}")
    return proc


def stop_process(proc: Optional[subprocess.Popen], port: int):
    """Gracefully terminate a process and ensure port is released."""
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        wait_for_port_closed(port)


def ensure_proxy_running() -> Optional[subprocess.Popen]:
    """Ensure fault proxy is running on port 8001."""
    if is_port_open(8001):
        return None
    proc = subprocess.Popen(["node", "index.js"], cwd="fault-proxy", stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not wait_for_port(8001):
        proc.terminate()
        raise RuntimeError("Fault proxy failed to start on port 8001")
    return proc


def reset_database():
    """Direct database truncation and seeding for clean scenario isolation."""
    with db_engine.connect() as conn:
        conn.execute(text("DELETE FROM idempotency_records;"))
        conn.execute(text("DELETE FROM payments;"))
        conn.execute(text("DELETE FROM bookings;"))
        conn.execute(text("DELETE FROM users;"))
        try:
            conn.execute(text("ALTER SEQUENCE bookings_id_seq RESTART WITH 1;"))
            conn.execute(text("ALTER SEQUENCE payments_id_seq RESTART WITH 1;"))
        except Exception:
            pass
        conn.execute(text("INSERT INTO users (id, balance, created_at) VALUES ('user_1', 1000.0, NOW());"))
        conn.commit()


def check_db_invariants(initial_balance: float = 1000.0) -> Dict[str, bool]:
    """Inspect PostgreSQL directly to evaluate Invariants I1, I2, and I3."""
    with db_engine.connect() as conn:
        bookings = conn.execute(text("SELECT id, user_id, slot_id, amount FROM bookings;")).mappings().all()
        payments = conn.execute(text("SELECT id, booking_id, amount FROM payments;")).mappings().all()
        balance = conn.execute(text("SELECT balance FROM users WHERE id = 'user_1';")).scalar()
        if balance is None:
            balance = 1000.0

        total_charged = sum(p["amount"] for p in payments)

        i1_violated = len(bookings) > 1
        i2_violated = len(payments) > 1
        i3_violated = balance < 0 or abs(balance - (initial_balance - total_charged)) > 0.01

        return {
            "I1": i1_violated,
            "I2": i2_violated,
            "I3": i3_violated,
        }


def run_experiment_suite():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    proxy_proc = ensure_proxy_running()

    versions = [
        ("Version A", False),  # IDEMPOTENCY=off
        ("Version B", True),   # IDEMPOTENCY=on
    ]

    # Store violation counts: key=(scenario, version, invariant) -> count
    counts = {}
    for sc_id, _, _, _ in SCENARIOS:
        for ver_name, _ in versions:
            for inv in ["I1", "I2", "I3"]:
                counts[(sc_id, ver_name, inv)] = 0

    print("=================================================================")
    print("        RetrySafe: Automated Experiment Execution Suite          ")
    print(f"        Seeds: {SEEDS} | Versions: Version A & Version B         ")
    print("=================================================================\n")

    client = RetryClient(base_url="http://127.0.0.1:8001", timeout=2.0, max_retries=3, retry_delay=0.05)

    try:
        for version_name, is_idempotent in versions:
            print(f"\n>>> Starting API for {version_name} (IDEMPOTENCY={'on' if is_idempotent else 'off'})...")
            api_proc = start_api_process(is_idempotent)

            try:
                for sc_id, fault_val, sc_name, fault_extra in SCENARIOS:
                    print(f"  Running Scenario {sc_id} ({sc_name})...")

                    for seed in SEEDS:
                        reset_database()

                        req_id = f"req_{version_name[:1]}_{sc_id}_{seed}"
                        idemp_key = f"idemp_{version_name[:1]}_{sc_id}_{seed}"
                        headers = {"X-Seed": str(seed)}
                        if fault_extra:
                            headers.update(fault_extra)

                        client.book(
                            user_id="user_1",
                            slot_id="slot_101",
                            amount=100.0,
                            request_id=req_id,
                            fault=fault_val,
                            fault_headers=headers,
                            idempotency_key=idemp_key,
                        )

                        if fault_val == "duplicate":
                            time.sleep(0.12)

                        inv_results = check_db_invariants(initial_balance=1000.0)
                        for inv, violated in inv_results.items():
                            if violated:
                                counts[(sc_id, version_name, inv)] += 1

            finally:
                print(f">>> Stopping API for {version_name}...")
                stop_process(api_proc, 8000)

    finally:
        if proxy_proc:
            stop_process(proxy_proc, 8001)

    # 1. Generate results/results.csv
    csv_rows = []
    for sc_id, _, _, _ in SCENARIOS:
        for version_name, _ in versions:
            for inv in ["I1", "I2", "I3"]:
                v_count = counts[(sc_id, version_name, inv)]
                csv_rows.append({
                    "scenario": sc_id,
                    "version": version_name,
                    "invariant": inv,
                    "violations": v_count
                })

    df = pd.DataFrame(csv_rows)
    csv_path = os.path.join(RESULTS_DIR, "results.csv")
    df.to_csv(csv_path, index=False)
    print(f"\n[Artifact] Results CSV saved to: {csv_path}")

    # 2. Generate results/before_after.png
    generate_before_after_chart(df)

    # 3. Print Summary Table
    print_summary_table(df)


def generate_before_after_chart(df: pd.DataFrame):
    """Create comparison bar chart (Version A vs Version B) of total violations."""
    chart_path = os.path.join(RESULTS_DIR, "before_after.png")

    scenario_labels = [sc_id for sc_id, _, _, _ in SCENARIOS]
    va_violations = []
    vb_violations = []

    for sc_id in scenario_labels:
        sub_a = df[(df["scenario"] == sc_id) & (df["version"] == "Version A")]
        sub_b = df[(df["scenario"] == sc_id) & (df["version"] == "Version B")]
        # Total violations across all invariants for this scenario
        va_violations.append(int(sub_a["violations"].sum()))
        vb_violations.append(int(sub_b["violations"].sum()))

    import numpy as np
    x = np.arange(len(scenario_labels))
    width = 0.35

    plt.figure(figsize=(9, 5))
    rects1 = plt.bar(x - width/2, va_violations, width, label="Before (Version A: Unprotected)", color="#d9534f")
    rects2 = plt.bar(x + width/2, vb_violations, width, label="After (Version B: Idempotent)", color="#5cb85c")

    plt.xlabel("Fault Scenario")
    plt.ylabel("Total Invariant Violations (Across Seeds)")
    plt.title("RetrySafe: Before vs. After Adding Idempotency Keys")
    plt.xticks(x, [s.replace("_", "\n") for s in scenario_labels])
    max_val = max(va_violations + vb_violations + [1])
    plt.yticks(range(0, max_val + 2))
    plt.legend()
    plt.grid(axis="y", linestyle="--", alpha=0.7)

    # Add count labels on bars
    for rect in rects1:
        h = rect.get_height()
        if h > 0:
            plt.text(rect.get_x() + rect.get_width()/2., h + 0.1, f"{int(h)}", ha='center', va='bottom', fontweight='bold')
    for rect in rects2:
        h = rect.get_height()
        plt.text(rect.get_x() + rect.get_width()/2., h + 0.1, f"{int(h)}", ha='center', va='bottom')

    plt.tight_layout()
    plt.savefig(chart_path, dpi=150)
    plt.close()
    print(f"[Artifact] Before/After chart saved to: {chart_path}")


def print_summary_table(df: pd.DataFrame):
    """Print an aligned, human-readable summary table to stdout."""
    print("\n" + "=" * 70)
    print("               SUMMARY EXPERIMENT RESULTS TABLE               ")
    print("=" * 70)
    pivot = df.pivot_table(index=["scenario", "version"], columns="invariant", values="violations", aggfunc="first").reset_index()
    pivot["Total Violations"] = pivot["I1"] + pivot["I2"] + pivot["I3"]
    pivot["Status"] = pivot["Total Violations"].apply(lambda v: "VULNERABLE" if v > 0 else "SAFE")

    header = f"{'Scenario':<20} | {'Version':<11} | {'I1':<4} | {'I2':<4} | {'I3':<4} | {'Total':<6} | {'Outcome'}"
    print(header)
    print("-" * len(header))
    for _, row in pivot.iterrows():
        print(
            f"{row['scenario']:<20} | {row['version']:<11} | {row['I1']:<4} | "
            f"{row['I2']:<4} | {row['I3']:<4} | {row['Total Violations']:<6} | {row['Status']}"
        )
    print("=" * 70 + "\n")


if __name__ == "__main__":
    run_experiment_suite()
