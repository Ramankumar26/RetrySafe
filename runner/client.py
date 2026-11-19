import time
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import httpx


@dataclass
class AttemptRecord:
    attempt_num: int
    timestamp: float
    status_code: Optional[int]
    error: Optional[str]
    response_data: Optional[Dict[str, Any]]
    duration_ms: float


@dataclass
class ClientResult:
    request_id: str
    success: bool
    total_attempts: int
    final_response: Optional[Dict[str, Any]]
    final_status_code: Optional[int]
    attempts: List[AttemptRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "success": self.success,
            "total_attempts": self.total_attempts,
            "final_status_code": self.final_status_code,
            "final_response": self.final_response,
            "attempts": [
                {
                    "attempt_num": a.attempt_num,
                    "timestamp": a.timestamp,
                    "status_code": a.status_code,
                    "error": a.error,
                    "response_data": a.response_data,
                    "duration_ms": round(a.duration_ms, 2),
                }
                for a in self.attempts
            ],
        }


class RetryClient:
    """
    HTTP client sending requests through the Fault Proxy on :8001.
    Maintains the SAME logical request ID across all retries.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8001",
        timeout: float = 2.0,
        max_retries: int = 3,
        retry_delay: float = 0.05,
    ):

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    def book(
        self,
        user_id: str,
        slot_id: str,
        amount: float,
        request_id: Optional[str] = None,
        fault: Optional[str] = None,
        fault_headers: Optional[Dict[str, str]] = None,
        idempotency_key: Optional[str] = None,
        fault_on_retries: bool = False,
    ) -> ClientResult:
        """
        Sends POST /book through the proxy with retry logic.
        Uses the exact same request_id (and idempotency_key if set) on every retry.
        """
        if not request_id:
            request_id = f"req_{uuid.uuid4().hex[:8]}"

        payload = {
            "user_id": user_id,
            "slot_id": slot_id,
            "amount": amount,
        }

        base_headers = {
            "Content-Type": "application/json",
            "X-Request-Id": request_id,
        }
        if idempotency_key:
            base_headers["Idempotency-Key"] = idempotency_key

        attempts: List[AttemptRecord] = []
        success = False
        final_response = None
        final_status = None

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(1, self.max_retries + 1):
                start_t = time.time()
                status_code = None
                error_msg = None
                data = None

                # Fault injection headers: by default applied on attempt 1
                attempt_headers = dict(base_headers)
                if attempt == 1 or fault_on_retries:
                    if fault:
                        attempt_headers["X-Fault"] = fault
                    if fault_headers:
                        attempt_headers.update(fault_headers)
                else:
                    # On subsequent retries, network works normally
                    attempt_headers["X-Fault"] = "none"

                try:
                    res = client.post(
                        f"{self.base_url}/book",
                        json=payload,
                        headers=attempt_headers,
                    )
                    status_code = res.status_code
                    if res.is_success:
                        success = True
                        data = res.json()
                        final_response = data
                        final_status = status_code
                        attempts.append(AttemptRecord(
                            attempt_num=attempt,
                            timestamp=start_t,
                            status_code=status_code,
                            error=None,
                            response_data=data,
                            duration_ms=(time.time() - start_t) * 1000,
                        ))
                        break
                    else:
                        error_msg = f"HTTP {res.status_code}: {res.text}"
                        final_status = status_code
                        try:
                            data = res.json()
                            final_response = data
                        except Exception:
                            pass
                except (
                    httpx.ConnectError,
                    httpx.ReadTimeout,
                    httpx.WriteTimeout,
                    httpx.RemoteProtocolError,
                    httpx.RequestError,
                ) as exc:
                    error_msg = f"{type(exc).__name__}: {str(exc)}"

                attempts.append(AttemptRecord(
                    attempt_num=attempt,
                    timestamp=start_t,
                    status_code=status_code,
                    error=error_msg,
                    response_data=data,
                    duration_ms=(time.time() - start_t) * 1000,
                ))

                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)

        return ClientResult(
            request_id=request_id,
            success=success,
            total_attempts=len(attempts),
            final_response=final_response,
            final_status_code=final_status,
            attempts=attempts,
        )
