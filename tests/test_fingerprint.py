import unittest

import server


class FakeRequest:
    """Mimics the header surface of starlette.requests.Request used by
    build_opencode_headers (headers.get + headers.items)."""

    def __init__(self, headers):
        self.headers = headers


class BuildOpencodeHeadersTests(unittest.TestCase):
    def test_fingerprint_values_match_verified_cli_identity(self):
        headers = server.build_opencode_headers(FakeRequest({}))
        self.assertEqual(headers["User-Agent"], server.OPENCODE_UA)
        self.assertEqual(headers["x-opencode-client"], "desktop")
        self.assertEqual(headers["x-opencode-project"], "/opencode")
        self.assertTrue(headers["x-opencode-session"].startswith("ses_"))
        self.assertTrue(headers["x-opencode-request"].startswith("req_"))
        self.assertTrue(headers["x-opencode-user"].startswith("usr_"))
        self.assertEqual(headers["x-user-id"], headers["x-opencode-user"])

    def test_ids_are_fresh_per_call(self):
        first = server.build_opencode_headers(FakeRequest({}))
        second = server.build_opencode_headers(FakeRequest({}))
        self.assertNotEqual(first["x-opencode-session"], second["x-opencode-session"])
        self.assertNotEqual(first["x-opencode-request"], second["x-opencode-request"])
        self.assertNotEqual(first["x-opencode-user"], second["x-opencode-user"])

    def test_loopback_x_real_ip_is_dropped(self):
        headers = server.build_opencode_headers(FakeRequest({"x-real-ip": " 127.0.0.1 "}))
        self.assertNotIn("x-real-ip", headers)

    def test_public_x_real_ip_is_forwarded_trimmed(self):
        headers = server.build_opencode_headers(FakeRequest({"x-real-ip": " 203.0.113.7 "}))
        self.assertEqual(headers.get("x-real-ip"), "203.0.113.7")

    def test_downstream_opencode_and_anthropic_headers_pass_through(self):
        hdrs = server.build_opencode_headers(FakeRequest({
            "x-opencode-custom": "keep-me",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "cookie": "session=abc",
        }))
        self.assertEqual(hdrs["x-opencode-custom"], "keep-me")
        self.assertEqual(hdrs["anthropic-beta"], "prompt-caching-2024-07-31")
        self.assertNotIn("cookie", hdrs)


if __name__ == "__main__":
    unittest.main()
