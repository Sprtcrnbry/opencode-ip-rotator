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
        self.assertEqual(headers["x-opencode-user"], "opencode-user")
        self.assertEqual(headers["x-user-id"], "opencode-user")

    def test_ids_are_fresh_per_call(self):
        first = server.build_opencode_headers(FakeRequest({}))
        second = server.build_opencode_headers(FakeRequest({}))
        self.assertNotEqual(first["x-opencode-session"], second["x-opencode-session"])
        self.assertNotEqual(first["x-opencode-request"], second["x-opencode-request"])

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

    def test_real_opencode_agent_headers_are_preserved(self):
        hdrs = server.build_opencode_headers(FakeRequest({
            "user-agent": "opencode/1.18.18 (vscode)",
            "x-opencode-client": "vscode-extension",
            "x-opencode-project": "/custom/workspace",
            "x-opencode-session": "ses_my_custom_session",
            "x-opencode-request": "req_my_custom_request",
            "x-opencode-user": "usr_my_custom_user",
            "x-safety-identifier": "safe_123",
            "authorization": "Bearer sk-custom-key",
        }))
        self.assertEqual(hdrs["User-Agent"], "opencode/1.18.18 (vscode)")
        self.assertEqual(hdrs["x-opencode-client"], "vscode-extension")
        self.assertEqual(hdrs["x-opencode-project"], "/custom/workspace")
        self.assertEqual(hdrs["x-opencode-session"], "ses_my_custom_session")
        self.assertEqual(hdrs["x-opencode-request"], "req_my_custom_request")
        self.assertEqual(hdrs["x-opencode-user"], "usr_my_custom_user")
        self.assertEqual(hdrs["x-user-id"], "usr_my_custom_user")
        self.assertEqual(hdrs["x-safety-identifier"], "safe_123")
        self.assertEqual(hdrs["Authorization"], "Bearer sk-custom-key")

    def test_dummy_authorization_maps_to_public(self):
        for dummy in ("Bearer dummy", "Bearer test", "Bearer null", "Bearer undefined", "Bearer any"):
            hdrs = server.build_opencode_headers(FakeRequest({"authorization": dummy}))
            self.assertEqual(hdrs["Authorization"], "Bearer public")

    def test_real_authorization_is_preserved(self):
        hdrs = server.build_opencode_headers(FakeRequest({"authorization": "Bearer sk-valid-account-key"}))
        self.assertEqual(hdrs["Authorization"], "Bearer sk-valid-account-key")


if __name__ == "__main__":
    unittest.main()
