import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import rotator
from rate_limits import classify_upstream_429


class FakeResponse:
    def __init__(self, payload, retry_after=None):
        self._payload = payload
        self.headers = {"retry-after": retry_after} if retry_after else {}
        self.text = "rate limited"

    def json(self):
        return self._payload


class RateLimitClassificationTests(unittest.TestCase):
    def test_quota_preserves_retry_after(self):
        response = FakeResponse({"error": {"type": "FreeUsageLimitError", "message": "quota"}}, "3600")
        category, retry_after, payload = classify_upstream_429(response)
        self.assertEqual(category, "quota")
        self.assertEqual(retry_after, 3600)
        self.assertEqual(payload["error"]["type"], "FreeUsageLimitError")

    def test_general_rate_limit_is_not_misclassified_as_ip_block(self):
        response = FakeResponse({"error": {"type": "RateLimitError", "message": "slow down"}}, "60")
        category, retry_after, _ = classify_upstream_429(response)
        self.assertEqual(category, "rate_limit")
        self.assertEqual(retry_after, 60)

    def test_invalid_retry_after_is_ignored(self):
        response = FakeResponse({"error": {"type": "RateLimitError"}}, "not-a-number")
        _, retry_after, _ = classify_upstream_429(response)
        self.assertIsNone(retry_after)

    def test_missing_lease_table_blocks_rotation_conservatively(self):
        with TemporaryDirectory() as directory:
            original_path = rotator.FLOW_LEASE_DB_PATH
            try:
                rotator.FLOW_LEASE_DB_PATH = Path(directory) / "metrics.db"
                self.assertFalse(rotator.has_active_flow_leases())
                connection = rotator.sqlite3.connect(str(rotator.FLOW_LEASE_DB_PATH))
                connection.close()
                self.assertTrue(rotator.has_active_flow_leases())
            finally:
                rotator.FLOW_LEASE_DB_PATH = original_path

    def test_upstream_rate_limit_response_and_rotation(self):
        import asyncio
        from unittest.mock import patch, MagicMock
        import server

        response = FakeResponse({"error": {"type": "RateLimitError", "message": "rate limit reached"}}, "10")
        with patch.object(server, "rotate_egress", return_value=(True, "1.2.3.4")) as mock_rotate, \
             patch.object(server, "record_warp_rotation") as mock_record:
            res = server.upstream_rate_limit_response(response, "test-model")
            self.assertEqual(res.status_code, 429)
            self.assertEqual(res.headers.get("x-rate-limit-reason"), "rate_limit")
            self.assertEqual(res.headers.get("retry-after"), "10")

            # Run event loop to execute the scheduled background task
            async def run_rotation():
                await server.schedule_rotation_on_429("test")
            asyncio.run(run_rotation())
            self.assertTrue(mock_rotate.called)


if __name__ == "__main__":
    unittest.main()
