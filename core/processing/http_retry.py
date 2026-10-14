# v2/core/processing/http_retry.py
# Shared Manatal HTTP helper: retries rate-limited (429) requests instead of
# letting the caller silently skip the candidate.

import time
import requests

# Pause between candidates / pages. Manatal starts answering 429 Too Many
# Requests after ~45 s at the old 0.2 s pace, so we go slower on purpose.
REQUEST_DELAY_SECONDS = 1.0

MAX_ATTEMPTS = 5                    # 1 try + 4 retries
BACKOFF_SECONDS = [2, 4, 8, 16]     # used when Manatal sends no Retry-After header
MAX_RETRY_AFTER_SECONDS = 60        # cap so a bad header can't hang the run


def _wait_seconds(response, retry_index):
    """Honour Manatal's Retry-After header when present, else exponential backoff."""
    header = response.headers.get('Retry-After') if response is not None else None
    if header:
        try:
            return min(max(float(header), 0), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return BACKOFF_SECONDS[min(retry_index, len(BACKOFF_SECONDS) - 1)]


def request_with_retry(method, url, *, retry_transient_errors=True, **kwargs):
    """
    Send a request and retry on 429 Too Many Requests.

    retry_transient_errors: also retry 5xx responses and connection errors /
        timeouts. Safe for GETs; pass False for writes that are not idempotent
        (e.g. POSTing a note), where a timed-out request may already have been
        applied and retrying could create a duplicate. A 429 is always retried
        because the server rejected the request without applying it.

    Returns the final response with raise_for_status() already called, so any
    error left after the retries surfaces as requests.exceptions.HTTPError.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        is_last = attempt == MAX_ATTEMPTS
        try:
            response = requests.request(method, url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if not retry_transient_errors or is_last:
                raise
            wait = _wait_seconds(None, attempt - 1)
            print(f"    ↻ {type(e).__name__} on {method} {url} — retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        retryable = response.status_code == 429 or (
            retry_transient_errors and 500 <= response.status_code < 600
        )
        if retryable and not is_last:
            wait = _wait_seconds(response, attempt - 1)
            print(f"    ↻ {response.status_code} on {method} {url} — retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        response.raise_for_status()
        return response
