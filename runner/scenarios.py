import os
import sys
import json
import time
from typing import Dict, Any, List, Optional
import httpx

# Ensure workspace root and runner are in sys.path
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from client import RetryClient, ClientResult
from explain import explain_scenario, load_proxy_logs


class ScenarioRunner:
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
        self.client = RetryClient(base_url=self.proxy_url, timeout=2.0, max_retries=3, retry_delay=0.05)


    def reset_system(self) -> bool:
        """Reset the Booking API database and the proxy log."""
        try:
            with httpx.Client(timeout=5.0) as http:
                # 1. Reset database
                res = http.post(f"{self.api_url}/reset")
                if res.status_code != 200:
                    print(f"[Warning] Failed to reset DB: {res.text}")
                    return False

                # 2. Reset proxy logs
                try:
                    http.post(f"{self.proxy_url}/fault/reset-log")
                except Exception:
                    pass
            return True
        except Exception as exc:
            print(f"[Warning] Error resetting system: {exc}")
            return False

    def fetch_db_state(self, user_id: str = "user_1") -> Dict[str, Any]:
        """Fetch current bookings and user balance via the API."""
        with httpx.Client(timeout=3.0) as http:
            bookings_res = http.get(f"{self.api_url}/bookings")
            bookings = bookings_res.json() if bookings_res.is_success else []

            balance_res = http.get(f"{self.api_url}/balance/{user_id}")
            balance = balance_res.json().get("balance", 0.0) if balance_res.is_success else 0.0

            return {
                "bookings": bookings,
                "payments": [{"booking_id": b.get("id"), "amount": b.get("amount")} for b in bookings],
                "balance": balance,
            }

    def check_invariants(
        self,
        db_state: Dict[str, Any],
        initial_balance: float = 1000.0
    ) -> Dict[str, Any]:
        """
        Evaluate Invariants I1, I2, I3 based on current DB state.
        I1: At most 1 confirmed booking per logical request.
        I2: One payment per booking, never charged twice.
        I3: Balance never negative, equals initial_balance - sum(charges).
        """
        bookings = db_state.get("bookings", [])
        balance = db_state.get("balance", 0.0)

        invariants_violated = []
        details = {}

        # I1: At most 1 booking per logical request
        if len(bookings) <= 1:
            details["I1"] = {"passed": True, "detail": f"Bookings count: {len(bookings)} (<= 1)"}
        else:
            details["I1"] = {"passed": False, "detail": f"Duplicate bookings created: {len(bookings)}"}
            invariants_violated.append("I1")

        # I2: Exactly one payment charge per logical booking
        total_charged = sum(b.get("amount", 0.0) for b in bookings)
        expected_charge = bookings[0].get("amount", 0.0) if bookings else 0.0
        if len(bookings) <= 1:
            details["I2"] = {"passed": True, "detail": "Charged once per logical booking"}
        else:
            details["I2"] = {
                "passed": False,
                "detail": f"User charged {len(bookings)} times (total: {total_charged}) for 1 request"
            }
            invariants_violated.append("I2")

        # I3: Balance never negative and equals initial - total_charged
        expected_balance = initial_balance - total_charged
        if balance >= 0 and abs(balance - expected_balance) < 0.01:
            details["I3"] = {
                "passed": True,
                "detail": f"Balance {balance} equals initial {initial_balance} - charges {total_charged}"
            }
        else:
            details["I3"] = {
                "passed": False,
                "detail": f"Balance mismatch: actual {balance}, expected {expected_balance}"
            }
            invariants_violated.append("I3")

        return {
            "invariants_violated": invariants_violated,
            "has_violation": len(invariants_violated) > 0,
            "details": details,
        }

    def run_scenario(
        self,
        scenario_id: str,
        name: str,
        fault: str,
        user_id: str = "user_1",
        slot_id: str = "slot_101",
        amount: float = 100.0,
        fault_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Runs a single fault scenario and records business-level results."""
        print(f"\nRunning Scenario [{scenario_id}]: {name} (Fault: {fault})...")

        # 1. Reset DB before scenario
        self.reset_system()

        # 2. Execute request through proxy
        request_id = f"R1_{scenario_id.lower()}"
        client_res = self.client.book(
            user_id=user_id,
            slot_id=slot_id,
            amount=amount,
            request_id=request_id,
            fault=fault,
            fault_headers=fault_headers,
        )

        # Allow slight delay for async duplicate requests if any
        if fault == "duplicate":
            time.sleep(0.15)

        # 3. Check DB state
        db_state = self.fetch_db_state(user_id=user_id)

        # 4. Invariant evaluation
        inv_check = self.check_invariants(db_state, initial_balance=1000.0)

        # 5. Extract proxy logs for this request
        proxy_logs = load_proxy_logs(self.proxy_log_path, request_id=request_id)

        # 6. Construct violation record
        record = {
            "scenario_id": scenario_id,
            "name": name,
            "fault": fault,
            "request_id": request_id,
            "client_result": client_res.to_dict(),
            "db_state": db_state,
            "invariants_violated": inv_check["invariants_violated"],
            "has_violation": inv_check["has_violation"],
            "invariant_details": inv_check["details"],
            "proxy_logs_count": len(proxy_logs),
        }

        # 7. Generate counterexample story
        story = explain_scenario(record, proxy_logs)
        record["story"] = story

        print(f"  Attempts: {client_res.total_attempts}")
        print(f"  Bookings: {len(db_state['bookings'])}, Balance: {db_state['balance']}")
        print(f"  Violations: {inv_check['invariants_violated'] if inv_check['invariants_violated'] else 'None'}")
        print(f"  Story: \"{story}\"")

        return record

    def run_all(self) -> List[Dict[str, Any]]:
        """Runs the entire suite: Baseline + Faults F1 to F4."""
        os.makedirs(self.results_dir, exist_ok=True)

        scenarios = [
            {
                "scenario_id": "F0",
                "name": "Baseline Normal Operation",
                "fault": "none",
            },
            {
                "scenario_id": "F1",
                "name": "Request Lost in Transit",
                "fault": "drop_request",
            },
            {
                "scenario_id": "F2",
                "name": "Response Lost after Commit",
                "fault": "drop_response",
            },
            {
                "scenario_id": "F3",
                "name": "Server Crash Mid-Transaction",
                "fault": "crash",
            },
            {
                "scenario_id": "F4",
                "name": "Duplicate Network Delivery",
                "fault": "duplicate",
                "fault_headers": {"X-Duplicate-Mode": "delay", "X-Duplicate-Delay": "50"},
            },
        ]

        results = []
        for s in scenarios:
            res = self.run_scenario(
                scenario_id=s["scenario_id"],
                name=s["name"],
                fault=s["fault"],
                fault_headers=s.get("fault_headers"),
            )
            results.append(res)

        # Save results as JSON
        output_file = os.path.join(self.results_dir, "scenario_results.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)

        print(f"\nAll scenarios completed. Results saved to {output_file}")
        return results


if __name__ == "__main__":
    runner = ScenarioRunner()
    runner.run_all()
