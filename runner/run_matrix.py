import os
import sys
import json
import time
from typing import Dict, Any, List
import httpx
import pandas as pd
import matplotlib.pyplot as plt
from sqlalchemy import create_engine, text

# Ensure runner and root are on sys.path
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "booking-api")))

from client import RetryClient
from explain import explain_scenario, load_proxy_logs


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://retrysafe:retrysafe@127.0.0.1:5432/retrysafe"
)
db_engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class MatrixRunner:
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8000",
        proxy_url: str = "http://127.0.0.1:8001",
        results_dir: str = "results",
        proxy_log_path: str = "fault-proxy/proxy.log",
    ):
        self.api_url = api_url.rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.results_dir = results_dir
        self.proxy_log_path = proxy_log_path

    def set_app_version(self, version: str):
        """Toggle app version via /config/idempotency."""
        enabled = (version.upper() == "VERSION B")
        with httpx.Client(timeout=5.0) as http:
            http.post(f"{self.api_url}/config/idempotency?enabled={str(enabled).lower()}")

    def reset_system(self):
        """Reset DB directly via SQL (FR-14) and proxy log."""
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

        try:
            with httpx.Client(timeout=2.0) as http:
                http.post(f"{self.proxy_url}/fault/reset-log")
        except Exception:
            pass

    def fetch_db_state(self, user_id: str = "user_1") -> Dict[str, Any]:
        """Direct database read after each scenario to check invariants (FR-14)."""
        with db_engine.connect() as conn:
            b_rows = conn.execute(
                text("SELECT id, user_id, slot_id, amount, status, created_at FROM bookings ORDER BY id ASC")
            ).mappings().all()
            bookings = [dict(r) for r in b_rows]
            # Convert datetime to string for JSON serialization
            for b in bookings:
                if "created_at" in b and b["created_at"]:
                    b["created_at"] = str(b["created_at"])

            p_rows = conn.execute(
                text("SELECT id, booking_id, amount, status FROM payments WHERE user_id = :u ORDER BY id ASC"),
                {"u": user_id}
            ).mappings().all()
            payments = [dict(r) for r in p_rows]

            balance = conn.execute(
                text("SELECT balance FROM users WHERE id = :u"),
                {"u": user_id}
            ).scalar()
            if balance is None:
                balance = 1000.0

            return {
                "bookings": bookings,
                "payments": payments,
                "balance": float(balance),
            }

    def check_invariants(self, db_state: Dict[str, Any], initial_balance: float = 1000.0) -> Dict[str, Any]:
        bookings = db_state.get("bookings", [])
        payments = db_state.get("payments", [])
        balance = db_state.get("balance", 0.0)
        total_charged = sum(p.get("amount", 0.0) for p in payments)

        i1_violated = len(bookings) > 1
        i2_violated = len(payments) > 1
        i3_violated = balance < 0 or abs(balance - (initial_balance - total_charged)) > 0.01

        violations = []
        if i1_violated:
            violations.append("I1")
        if i2_violated:
            violations.append("I2")
        if i3_violated:
            violations.append("I3")

        return {
            "I1_violated": i1_violated,
            "I2_violated": i2_violated,
            "I3_violated": i3_violated,
            "invariants_violated": violations,
            "any_violated": len(violations) > 0,
        }

    def run_matrix(self) -> List[Dict[str, Any]]:
        os.makedirs(self.results_dir, exist_ok=True)

        versions = ["Version A", "Version B"]
        faults = [
            ("F0", "none", "Baseline"),
            ("F1", "drop_request", "Request Lost"),
            ("F2", "drop_response", "Response Lost"),
            ("F3", "crash", "Server Crash"),
            ("F4", "duplicate", "Duplicate Delivery"),
        ]
        retry_policies = [
            ("no_retry", 1),
            ("retry_3", 3),
        ]

        matrix_results = []
        total_runs = len(versions) * len(faults) * len(retry_policies)
        run_idx = 0

        print(f"=== Starting Execution Matrix ({total_runs} combinations) ===")

        for version in versions:
            self.set_app_version(version)
            is_version_b = (version == "Version B")

            for fault_id, fault_val, fault_desc in faults:
                for retry_name, max_retries in retry_policies:
                    run_idx += 1
                    self.reset_system()

                    client = RetryClient(
                        base_url=self.proxy_url,
                        timeout=2.0,
                        max_retries=max_retries,
                        retry_delay=0.05
                    )

                    v_code = "A" if version == "Version A" else "B"
                    req_id = f"R1_{v_code}_{fault_id}_{retry_name}"
                    idemp_key = f"key_{req_id}" if is_version_b else None
                    fault_headers = (
                        {"X-Duplicate-Mode": "delay", "X-Duplicate-Delay": "50"}
                        if fault_val == "duplicate"
                        else None
                    )

                    client_res = client.book(
                        user_id="user_1",
                        slot_id="slot_101",
                        amount=100.0,
                        request_id=req_id,
                        fault=fault_val,
                        fault_headers=fault_headers,
                        idempotency_key=idemp_key,
                    )

                    if fault_val == "duplicate":
                        time.sleep(0.12)

                    db_state = self.fetch_db_state("user_1")
                    inv_check = self.check_invariants(db_state, 1000.0)

                    record = {
                        "scenario_id": f"{v_code}_{fault_id}_{retry_name}",
                        "version": version,
                        "fault_id": fault_id,
                        "fault": fault_val,
                        "fault_desc": fault_desc,
                        "retry_policy": retry_name,
                        "max_retries": max_retries,
                        "request_id": req_id,
                        "total_attempts": client_res.total_attempts,
                        "client_success": client_res.success,
                        "bookings_count": len(db_state["bookings"]),
                        "payments_count": len(db_state["payments"]),
                        "final_balance": db_state["balance"],
                        "I1_violated": inv_check["I1_violated"],
                        "I2_violated": inv_check["I2_violated"],
                        "I3_violated": inv_check["I3_violated"],
                        "any_violated": inv_check["any_violated"],
                        "invariants_violated": inv_check["invariants_violated"],
                        "client_result": client_res.to_dict(),
                        "db_state": db_state,
                    }

                    proxy_logs = load_proxy_logs(self.proxy_log_path, request_id=req_id)
                    record["story"] = explain_scenario(record, proxy_logs)
                    matrix_results.append(record)

                    status_str = "VIOLATION" if inv_check["any_violated"] else "SAFE"
                    print(
                        f"[{run_idx:02d}/{total_runs:02d}] {version} | {fault_id} ({fault_desc:18s}) | "
                        f"{retry_name:8s} -> Bookings: {len(db_state['bookings'])}, Balance: {db_state['balance']} [{status_str}]"
                    )

        # 1. Save JSON
        json_path = os.path.join(self.results_dir, "matrix_results.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(matrix_results, f, indent=2)

        # 2. Save CSV summary using pandas
        summary_rows = []
        for r in matrix_results:
            summary_rows.append({
                "Version": r["version"],
                "Fault ID": r["fault_id"],
                "Fault Description": r["fault_desc"],
                "Retry Policy": r["retry_policy"],
                "Attempts": r["total_attempts"],
                "Bookings": r["bookings_count"],
                "Balance": r["final_balance"],
                "I1 Violated": "YES" if r["I1_violated"] else "NO",
                "I2 Violated": "YES" if r["I2_violated"] else "NO",
                "I3 Violated": "YES" if r["I3_violated"] else "NO",
                "Any Broken": "YES" if r["any_violated"] else "NO",
                "Story": r["story"]
            })
        df = pd.DataFrame(summary_rows)
        csv_path = os.path.join(self.results_dir, "summary_table.csv")
        df.to_csv(csv_path, index=False)
        print(f"\nSaved CSV summary to {csv_path}")

        # 3. Generate Chart: Version A vs Version B Violations by Fault
        self.generate_chart(df)

        return matrix_results

    def generate_chart(self, df: pd.DataFrame):
        """Generates a comparison bar chart between Version A and Version B."""
        chart_path = os.path.join(self.results_dir, "version_a_vs_b.png")

        fault_order = ["F0", "F1", "F2", "F3", "F4"]
        labels = [f"{fid}" for fid in fault_order]

        va_counts = []
        vb_counts = []

        for fid in fault_order:
            sub_a = df[(df["Version"] == "Version A") & (df["Fault ID"] == fid)]
            sub_b = df[(df["Version"] == "Version B") & (df["Fault ID"] == fid)]
            va_counts.append(int((sub_a["Any Broken"] == "YES").sum()))
            vb_counts.append(int((sub_b["Any Broken"] == "YES").sum()))

        import numpy as np
        x = np.arange(len(fault_order))
        width = 0.35

        plt.figure(figsize=(9, 5))
        plt.bar(x - width/2, va_counts, width, label="Version A (Unprotected)", color="#d9534f")
        plt.bar(x + width/2, vb_counts, width, label="Version B (Idempotent)", color="#5cb85c")

        plt.xlabel("Fault Type (F0: Baseline, F1: Drop Req, F2: Drop Res, F3: Crash, F4: Duplicate)")
        plt.ylabel("Invariant Violations Count")
        plt.title("RetrySafe: Business Invariant Violations (Version A vs Version B)")
        plt.xticks(x, labels)
        plt.yticks(range(0, max(max(va_counts), 1) + 2))
        plt.legend()
        plt.grid(axis="y", linestyle="--", alpha=0.7)
        plt.tight_layout()
        plt.savefig(chart_path, dpi=150)
        plt.close()
        print(f"Saved comparison chart to {chart_path}")


if __name__ == "__main__":
    runner = MatrixRunner()
    runner.run_matrix()
