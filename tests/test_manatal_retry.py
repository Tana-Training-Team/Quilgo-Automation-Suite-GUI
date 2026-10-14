"""
Tests for Manatal rate-limit handling (HTTP 429).

Run from the project root:
    python -m unittest discover -s tests -p "test_*.py" -v

A real run lost 15 of 62 fellows because Manatal answered 429 Too Many Requests
and the fetcher skipped them. These tests fake Manatal's responses to check that
429s are retried, and that anything still failing stops the run instead of
silently dropping fellows.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import processor
from core.processing import http_retry, manatal_fetcher
from core.processing.manatal_fetcher import ManatalFetchIncomplete, fetch_manatal_profiles


def _resp(status, json_body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = (b"null" if json_body is None else __import__("json").dumps(json_body).encode())
    r.url = "https://api.manatal.com/fake"
    return r


class FakeManatal:
    """
    Serves one tech job with `n` candidates in the target stage and no non-tech
    candidates. `failures` maps a URL substring to a list of status codes to
    return (in order) before answering normally.
    """

    def __init__(self, n, failures=None):
        self.n = n
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        for key, statuses in self.failures.items():
            if key in url and statuses:
                status = statuses.pop(0)
                if isinstance(status, Exception):
                    raise status
                return _resp(status, headers={"Retry-After": "3"} if status == 429 else None)

        tech = manatal_fetcher.JOB_CONFIGS["tech"]
        if f"/jobs/{tech['job_id']}/matches/" in url:
            stage = next(iter(tech["target_stage_ids"]))
            results = [{"id": 1000 + i, "candidate": i, "is_active": True, "stage": {"id": stage}}
                       for i in range(1, self.n + 1)]
            return _resp(200, {"results": results, "next": None})
        if "/jobs/" in url:
            return _resp(200, {"results": [], "next": None})
        if "/candidates/" in url:
            cid = int(url.rstrip("/").split("/")[-1])
            return _resp(200, {"id": cid, "full_name": f"Fellow {cid}", "email": f"f{cid}@x.io"})
        if "/notes/" in url:
            return _resp(200, [])
        return _resp(201, {})


class RetryTests(unittest.TestCase):
    def setUp(self):
        # Never actually wait during tests; record the waits instead.
        self.sleeps = []
        for target in (http_retry.time, manatal_fetcher.time):
            p = mock.patch.object(target, "sleep", side_effect=self.sleeps.append)
            p.start()
            self.addCleanup(p.stop)

    def _patch_manatal(self, fake):
        p = mock.patch.object(http_retry.requests, "request", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)

    def test_rate_limited_candidates_are_retried_not_dropped(self):
        # Candidates 1 and 2 hit 429 on the profile call, candidate 3 on the notes call.
        fake = FakeManatal(5, failures={
            "/candidates/1/": [429, 429],
            "/candidates/2/": [429],
            "/matches/1003/notes/": [429, 429, 429],
        })
        self._patch_manatal(fake)

        df, raw = fetch_manatal_profiles("key")

        self.assertEqual(len(raw), 5, "every fellow in the stage must be fetched")
        self.assertEqual(sorted(p["id"] for p in raw), [1, 2, 3, 4, 5])
        self.assertIn(3, self.sleeps, "Retry-After header (3s) should be honoured")

    def test_pause_between_candidates_is_one_second(self):
        self._patch_manatal(FakeManatal(3))
        fetch_manatal_profiles("key")
        self.assertEqual(http_retry.REQUEST_DELAY_SECONDS, 1.0)
        self.assertGreaterEqual(self.sleeps.count(1.0), 3)

    def test_persistent_429_stops_the_run(self):
        fake = FakeManatal(4, failures={"/candidates/2/": [429] * http_retry.MAX_ATTEMPTS})
        self._patch_manatal(fake)
        with self.assertRaises(ManatalFetchIncomplete) as ctx:
            fetch_manatal_profiles("key")
        self.assertIn("1 of 4", str(ctx.exception))

    def test_match_page_failure_stops_the_run(self):
        fake = FakeManatal(4, failures={"/jobs/": [503] * http_retry.MAX_ATTEMPTS})
        self._patch_manatal(fake)
        with self.assertRaises(ManatalFetchIncomplete):
            fetch_manatal_profiles("key")

    def test_processor_fails_part2_and_does_not_write_partial_cache(self):
        fake = FakeManatal(4, failures={"/candidates/2/": [429] * http_retry.MAX_ATTEMPTS})
        self._patch_manatal(fake)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "manatal_profiles_cache.json"
            with mock.patch.object(processor.config, "MANATAL_CACHE_FILE", cache), \
                 mock.patch.object(processor.config, "DOWNLOADS_DIR", Path(tmp)):
                results, ok = processor.run_or_rerun_processing(
                    use_cache=False, api_key="key", project_root=Path(tmp),
                    get_manual_review_decision=lambda *a: ("skip", ""),
                )
            self.assertFalse(ok)
            self.assertEqual(results, [])
            self.assertFalse(cache.exists(), "a partial candidate list must not be cached")

    def test_note_post_retries_429_but_not_timeouts(self):
        # 429 → retried (Manatal rejected it, nothing was created).
        fake = FakeManatal(0, failures={"/notes/": [429]})
        self._patch_manatal(fake)
        http_retry.request_with_retry("POST", "https://x/matches/1/notes/", retry_transient_errors=False)
        self.assertEqual(len(fake.calls), 2)

        # Timeout → NOT retried (the note may already exist; a retry could duplicate it).
        fake = FakeManatal(0, failures={"/notes/": [requests.exceptions.Timeout("slow")]})
        self._patch_manatal(fake)
        with self.assertRaises(requests.exceptions.Timeout):
            http_retry.request_with_retry("POST", "https://x/matches/1/notes/", retry_transient_errors=False)
        self.assertEqual(len(fake.calls), 1)


if __name__ == "__main__":
    unittest.main()
