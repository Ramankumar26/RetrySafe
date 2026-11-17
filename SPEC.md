# RetrySafe: Product Requirements Document

Mini project | Version 1.0 | Stack: FastAPI + Node/Express + Python test runner

## 1. Overview

RetrySafe is a developer-facing testing tool that checks whether unreliable networks corrupt user intent. It injects failures at different stages of a booking/payment transaction and verifies business invariants such as "one confirmed booking per request". When an invariant breaks, RetrySafe explains the failure as a short story in terms of user intent, not network errors.

Example output:

> Request R1 succeeded on the server, its acknowledgment was lost, the client retried, and the retry created a duplicate booking. Invariant "one booking per request" violated.

## 2. Problem Statement

A server completes an operation but the response never reaches the user. The user (or client library) retries and accidentally creates two bookings, two payments, or two submissions. Existing tools (chaos engineering, Jepsen-style checkers, Toxiproxy) report network or consistency errors but rarely explain the business-level consequence in a way a student or small team can understand.

## 3. Goals and Non-Goals

### Goals

- G1: Build a small mock booking workflow that is deliberately vulnerable to retry bugs (Version A).
- G2: Inject network faults at defined transaction stages using an external proxy.
- G3: Automatically check business invariants after every scenario.
- G4: Produce a human-readable counterexample story for each violation, with the smallest failing sequence.
- G5: Demonstrate before/after results by adding idempotency keys (Version B).
- G6: Produce reproducible numbers for the project report.

### Non-Goals

- No experiments against real payment services, ever. Everything is local and mocked.
- No machine learning. An LLM is out of scope unless it demonstrably improves test selection.
- No claim of a general distributed-systems checker. Scope is one workflow.
- No UI is required. A dashboard is optional.

## 4. Target Users

- Primary: the student author, who needs a working, demonstrable project and report.
- Secondary: developers of small booking, payment, or form-submission services who want a simple retry-safety test.
- Evaluators: examiners who should understand the problem from a one-line example.

## 5. Scope

| Item | Limit |
| --- | --- |
| Workflow | One: seat/slot booking with a mock payment step |
| Failure types | 4 |
| Invariants | 3 |
| App versions | 2 (without and with idempotency keys) |
| Database | PostgreSQL |

## 6. System Architecture

```
Runner (Python)  ->  Fault Proxy (Node/Express, :8001)  ->  Booking API (FastAPI, :8000)  ->  PostgreSQL
```

- The runner always sends traffic to the proxy, never directly to the API.
- The proxy is a separate process so that network failure is realistic and the proxy is reusable with any backend.
- The runner reads the database after each scenario to check invariants.

## 7. Tech Stack

| Component | Technology | Reason |
| --- | --- | --- |
| Booking API | FastAPI (Python) | Fast to build, easy for AI-assisted coding |
| Database | PostgreSQL (via Docker Compose) | Real transactions, unique constraints, and row locking, which idempotency relies on |
| Fault proxy | Node.js + Express + http-proxy-middleware | Simple, controllable proxy |
| Runner and checker | Python, pytest, httpx | Single language for tests |
| Property-based testing | Hypothesis | Random fault sequences and shrinking to minimal counterexamples |
| Results | pandas, matplotlib | Tables and charts for the report |
| Optional | Streamlit dashboard | Demo polish |

## 8. Functional Requirements

### 8.1 Booking API (FastAPI)

- FR-1: `POST /book` accepts `user_id`, `slot_id`, and `amount`, creates a booking, and records a mock payment charge.
- FR-2: `GET /bookings` returns all bookings for inspection.
- FR-3: `GET /balance/{user_id}` returns the mock balance.
- FR-4: `POST /reset` clears all data for repeatable runs (test mode only).
- FR-5 (Version A): no duplicate protection. Retried requests create new bookings.
- FR-6 (Version B): accepts an `Idempotency-Key` header. The same key returns the original result and never creates a second booking or charge. Implemented with a unique constraint and a transaction.
- FR-7: A configurable crash point (for example, after charge but before commit) to simulate a server failure mid-transaction.

### 8.2 Fault Proxy (Express)

The proxy must support these faults, selected per request through a control endpoint or config file:

| ID | Fault | Behavior |
| --- | --- | --- |
| F1 | Request lost | Request is dropped and never reaches the server |
| F2 | Response lost | Server processes the request, but the response is dropped or the connection is closed |
| F3 | Server crash mid-transaction | Proxy triggers the API crash point (FR-7) |
| F4 | Duplicate or parallel delivery | Same request is forwarded twice, simultaneously or with a short delay |

- FR-8: Fault selection must be deterministic from a seed so that every run is reproducible.
- FR-9: The proxy logs every request, forward, drop, and response with timestamps and a request ID.

### 8.3 Scenario Runner (Python)

- FR-10: A client that sends booking requests through the proxy, with configurable timeout and retry policy (for example, retry up to 3 times).
- FR-11: Run each fault type against each app version and each retry policy.
- FR-12: Use Hypothesis to generate random sequences of faults and retries, and shrink failures to the smallest sequence.
- FR-13: Save raw results as JSON/CSV.

### 8.4 Invariant Checker

| ID | Invariant |
| --- | --- |
| I1 | One logical request results in at most one confirmed booking |
| I2 | One payment per booking, never charged twice |
| I3 | User balance never goes negative and equals initial balance minus the sum of confirmed charges |

- FR-14: After each scenario, query the database and evaluate I1 to I3.
- FR-15: Each violation produces a structured record: scenario ID, fault type, request timeline, and the violated invariant.

### 8.5 Explanation Generator

- FR-16: Convert each violation record into a plain-language story using fixed templates (no LLM needed).
- FR-17: Include the minimal failing sequence, for example: Send request -> server commits -> response lost -> client timeout -> retry -> duplicate booking.

### 8.6 Reporting

- FR-18: Produce a results table: scenario x version x invariant broken (yes/no) x count.
- FR-19: Produce a before/after bar chart (Version A vs Version B).
- FR-20: Export results to `results/` for direct use in the project report.

## 9. Experiments and Metrics

| Experiment | Question | Metric |
| --- | --- | --- |
| E1 | Does Version A break under each fault? | Violations per fault type |
| E2 | Does idempotency in Version B fix them? | Violations after fix (target: 0 for F1, F2, F4) |
| E3 | Does F3 (crash mid-transaction) still cause trouble in Version B? | Remaining violations, with analysis |
| E4 | Does random generation find failures that fixed scenarios miss? | Failures found by Hypothesis vs fixed list |
| E5 | How small are the counterexamples? | Length of shrunk failing sequence |

## 10. Non-Functional Requirements

- Reproducibility: same seed gives the same result.
- Speed: the full scenario suite runs in under 5 minutes on a laptop.
- Safety: no network calls outside localhost.
- Simplicity: setup in at most 3 commands (see README).
- Readability: every report line is understandable to a non-expert.

## 11. Repository Structure

```
retrysafe/
  booking-api/     # FastAPI + PostgreSQL
  fault-proxy/     # Node + Express
  runner/          # scenarios, invariants, explanations
  results/         # tables, charts
  SPEC.md          # this document
  README.md
```

## 12. Milestones (10-day plan)

| Days | Deliverable |
| --- | --- |
| 1-2 | Setup, spec approved, project skeleton |
| 3-4 | Booking API (Version A) with tests |
| 4-5 | Fault proxy with F1 to F4 |
| 5-6 | Runner and invariant checker |
| 6-7 | Version B (idempotency) and experiments |
| 7-8 | Hypothesis-based exploration, charts |
| 8-9 | Report writing |
| 10 | Demo script, README, polish |

Fallback: if time runs short, implement fault injection as FastAPI middleware and skip the Express proxy.

## 13. Risks and Mitigations

| Risk | Mitigation |
| --- | --- |
| Two runtimes cause setup bugs | Pin versions, provide one start script |
| AI-generated code breaks other modules | One task at a time, commit after each step, run tests |
| Flaky results from timing | Seeded faults and fixed delays |
| Scope creep | Hold to one workflow, 4 faults, 3 invariants |
| Examiner asks "why not existing tools?" | Differentiate on business-level counterexamples (see Section 14) |

## 14. Related Work and Differentiation

Chaos engineering, Jepsen-style checkers, and fault-injection tools such as Toxiproxy already exist. RetrySafe does not replace them. Its contribution is a small, understandable harness that expresses failures as business-level counterexamples tied to user intent, plus a reproducible before/after demonstration of idempotency.

## 15. Limitations (to state in the report)

- Covers one workflow and four fault types, not all distributed failures.
- Mock payments only; behavior of real payment providers may differ.
- Passing the tests does not prove the system is retry-safe in general.
- A single PostgreSQL instance is used; replication, failover, and multi-node consistency issues are out of scope.

## 16. Acceptance Criteria

- Version A violates at least I1 under F2 and F4, and the tool prints the explanatory story.
- Version B shows zero violations for F1, F2, and F4.
- Results are reproducible from a fixed seed.
- Charts and tables are generated automatically into `results/`.
- The README lets someone else run the whole project.
- The report includes introduction, design, experiments, results, and limitations.
