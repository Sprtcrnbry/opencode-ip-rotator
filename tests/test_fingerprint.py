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
        self.assertEqual(headers["Authorization"], "Bearer public")

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

    def test_downstream_opencode_headers_pass_through_and_extraneous_dropped(self):
        hdrs = server.build_opencode_headers(FakeRequest({
            "x-opencode-custom": "keep-me",
            "anthropic-beta": "prompt-caching-2024-07-31",
            "openai-organization": "org-123",
            "x-session-affinity": "ses-aff-123",
            "cookie": "session=abc",
        }))
        self.assertEqual(hdrs["x-opencode-custom"], "keep-me")
        self.assertNotIn("anthropic-beta", hdrs)
        self.assertNotIn("openai-organization", hdrs)
        self.assertNotIn("x-session-affinity", hdrs)
        self.assertNotIn("cookie", hdrs)

    def test_real_opencode_agent_headers_are_preserved(self):
        hdrs = server.build_opencode_headers(FakeRequest({
            "user-agent": "opencode/1.18.18 (vscode)",
            "x-opencode-client": "vscode-extension",
            "x-opencode-project": "/custom/workspace",
            "x-opencode-session": "ses_my_custom_session",
            "x-opencode-request": "req_my_custom_request",
            "authorization": "Bearer sk-custom-key",
        }))
        self.assertEqual(hdrs["User-Agent"], "opencode/1.18.18 (vscode)")
        self.assertEqual(hdrs["x-opencode-client"], "vscode-extension")
        self.assertEqual(hdrs["x-opencode-project"], "/custom/workspace")
        self.assertEqual(hdrs["x-opencode-session"], "ses_my_custom_session")
        self.assertEqual(hdrs["x-opencode-request"], "req_my_custom_request")
        self.assertEqual(hdrs["Authorization"], "Bearer sk-custom-key")

    def test_dummy_authorization_maps_to_public(self):
        for dummy in ("Bearer dummy", "Bearer test", "Bearer null", "Bearer undefined", "Bearer any"):
            hdrs = server.build_opencode_headers(FakeRequest({"authorization": dummy}))
            self.assertEqual(hdrs["Authorization"], "Bearer public")

    def test_real_authorization_is_preserved(self):
        hdrs = server.build_opencode_headers(FakeRequest({"authorization": "Bearer sk-valid-account-key"}))
        self.assertEqual(hdrs["Authorization"], "Bearer sk-valid-account-key")

    def test_normalize_upstream_model_name_strips_routing_prefixes(self):
        cases = [
            ("opencode/muse-spark-1.2-contributor-free", "muse-spark-1.2-contributor-free"),
            ("ocf/big-pickle", "big-pickle"),
            ("zen/deepseek-v4-flash-free", "deepseek-v4-flash-free"),
            ("openai/gpt-4o", "gpt-4o"),
            ("nous/tencent/hy3-free", "hy3-free"),
            ("muse-spark-1.2-contributor-free", "muse-spark-1.2-contributor-free"),
            ("", "deepseek-v4-flash-free"),
            (None, "deepseek-v4-flash-free"),
        ]
    def test_optimize_payload_drops_orphaned_tools(self):
        payload = {
            "model": "muse-spark-1.2-contributor-free",
            "messages": [
                {"role": "system", "content": "prompt"},
                {"role": "user", "content": "hi"},
                {"role": "tool", "tool_call_id": "orphaned_call_123", "content": "orphan"},
                {"role": "assistant", "content": "call me", "tool_calls": [{"id": "valid_call_456", "type": "function", "function": {"name": "f"}}]},
                {"role": "tool", "tool_call_id": "valid_call_456", "content": "valid result"}
            ]
        }
        optimized = server.optimize_payload_for_upstream(payload)
        roles = [m["role"] for m in optimized["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(optimized["messages"][-1]["tool_call_id"], "valid_call_456")


if __name__ == "__main__":
    unittest.main()
