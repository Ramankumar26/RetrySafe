import json
import os
from typing import Dict, Any, List, Optional


def load_proxy_logs(log_path: str = "fault-proxy/proxy.log", request_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Loads and filters proxy JSON line logs for a specific request ID."""
    if not os.path.exists(log_path):
        return []
    logs = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if request_id is None or entry.get("requestId") == request_id:
                    logs.append(entry)
            except Exception:
                continue
    return logs


def explain_scenario(record: Dict[str, Any], proxy_logs: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Converts a scenario result/violation record and proxy logs into a plain-language
    counterexample story using fixed templates (no LLM).
    """
    fault = record.get("fault", "none")
    req_id = record.get("request_id", "R1")
    attempts = record.get("client_result", {}).get("attempts", [])
    total_attempts = record.get("client_result", {}).get("total_attempts", len(attempts))
    db_state = record.get("db_state", {})
    bookings = db_state.get("bookings", [])
    payments = db_state.get("payments", [])
    invariants_violated = record.get("invariants_violated", [])

    booking_ids_str = ", ".join(f"#{b.get('id')}" for b in bookings) if bookings else "none"
    booking_count = len(bookings)
    payment_count = len(payments)

    # 1. Fault F2: Response lost (drop_response)
    if fault == "drop_response":
        if "I1" in invariants_violated or booking_count > 1:
            return (
                f"Request {req_id} succeeded on the server, the acknowledgment was lost, "
                f"the client retried, the retry created a duplicate booking ({booking_ids_str}). "
                f"Invariant I1 (one booking per request) violated."
            )
        else:
            return (
                f"Request {req_id} had its server response lost. The client retried and completed. "
                f"Total bookings: {booking_count}. No invariants violated."
            )

    # 2. Fault F4: Duplicate / parallel delivery
    if fault == "duplicate":
        if "I1" in invariants_violated or booking_count > 1:
            return (
                f"Request {req_id} was duplicated in transit and forwarded twice to the server. "
                f"Because the server has no duplicate protection, both executions completed, "
                f"creating {booking_count} bookings ({booking_ids_str}) and charging the user {payment_count} times. "
                f"Invariant I1 violated."
            )
        else:
            return (
                f"Request {req_id} was duplicated in transit, but server deduplicated or processed cleanly. "
                f"Invariant I1 preserved."
            )

    # 3. Fault F1: Request lost (drop_request)
    if fault == "drop_request":
        if total_attempts > 1 and booking_count == 1:
            return (
                f"Request {req_id} was dropped before reaching the server on attempt 1. "
                f"The client detected the timeout/connection drop and retried. "
                f"The retry reached the server and confirmed booking {booking_ids_str}. "
                f"Invariants preserved."
            )
        elif booking_count == 0:
            return (
                f"Request {req_id} was dropped before reaching the server on all {total_attempts} attempts. "
                f"No booking was created and no charge was processed. "
                f"Invariants preserved."
            )
        else:
            return (
                f"Request {req_id} experienced request drop fault. "
                f"Final state: {booking_count} bookings created. "
                f"Invariants violated: {', '.join(invariants_violated) if invariants_violated else 'None'}."
            )

    # 4. Fault F3: Server crash mid-transaction
    if fault == "crash":
        if total_attempts > 1 and booking_count == 1:
            return (
                f"Request {req_id} triggered a server crash mid-transaction after the charge was written. "
                f"The transaction rolled back atomically. The client retried, and the retry succeeded "
                f"with booking {booking_ids_str}. Invariants preserved."
            )
        elif booking_count == 0:
            return (
                f"Request {req_id} triggered a server crash mid-transaction, and the transaction was rolled back. "
                f"No booking or payment remained. Invariants preserved."
            )
        else:
            return (
                f"Request {req_id} crashed mid-transaction. "
                f"Invariants violated: {', '.join(invariants_violated) if invariants_violated else 'None'}."
            )

    # 5. Baseline / None
    if fault == "none":
        if not invariants_violated and booking_count == 1:
            return (
                f"Request {req_id} succeeded on the server on the first attempt without network faults. "
                f"Confirmed booking {booking_ids_str}. All invariants satisfied."
            )
        else:
            return (
                f"Request {req_id} baseline run completed. "
                f"Bookings: {booking_count}, Payments: {payment_count}. "
                f"Invariants violated: {', '.join(invariants_violated) if invariants_violated else 'None'}."
            )

    # Generic Fallback
    violated_str = ", ".join(invariants_violated) if invariants_violated else "None"
    return (
        f"Request {req_id} with fault '{fault}' resulted in {booking_count} bookings and "
        f"{payment_count} payments. Invariants violated: {violated_str}."
    )


def explain_counterexample(record: Dict[str, Any], proxy_logs: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Explains a Hypothesis-shrunk counterexample sequence, combining
    step-by-step attempt trace with the business invariant explanation story.
    """
    story = explain_scenario(record, proxy_logs)
    shrunk_seq = record.get("shrunk_sequence", [])
    if shrunk_seq:
        trace_lines = []
        for i, step in enumerate(shrunk_seq, 1):
            f = step.get("fault", "none")
            amt = step.get("amount", 100.0)
            trace_lines.append(f"    Step {i}: fault={f}, amount={amt}")
        return f"{story}\n  Shrunk Sequence ({len(shrunk_seq)} step{'s' if len(shrunk_seq) > 1 else ''}):\n" + "\n".join(trace_lines)
    return story

