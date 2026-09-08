import os
import unittest
from unittest.mock import MagicMock, patch

import rotator
import server


class FakeCurlResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def json(self):
        import json
        return json.loads(self.text)


class RotatorIpGuardTests(unittest.TestCase):
    def setUp(self):
        self.orig_host_ip = rotator.HOST_DIRECT_IP
        self.orig_custom_proxy = os.environ.get("CUSTOM_OUTBOUND_PROXY")

    def tearDown(self):
        rotator.HOST_DIRECT_IP = self.orig_host_ip
        if self.orig_custom_proxy is not None:
            os.environ["CUSTOM_OUTBOUND_PROXY"] = self.orig_custom_proxy
        else:
            os.environ.pop("CUSTOM_OUTBOUND_PROXY", None)

    def test_get_public_ip_rejects_warp_off(self):
        trace_payload = "ip=141.11.190.114\nwarp=off\n"
        with patch("curl_cffi.requests.get", return_value=FakeCurlResponse(trace_payload)):
            ip = rotator.get_public_ip(require_warp=True)
            self.assertIsNone(ip)

    def test_get_public_ip_accepts_warp_on(self):
        trace_payload = "ip=104.28.213.127\nwarp=on\n"
        with patch("curl_cffi.requests.get", return_value=FakeCurlResponse(trace_payload)):
            ip = rotator.get_public_ip(require_warp=True)
            self.assertEqual(ip, "104.28.213.127")

    def test_get_public_ip_accepts_warp_plus(self):
        trace_payload = "ip=104.28.213.128\nwarp=plus\n"
        with patch("curl_cffi.requests.get", return_value=FakeCurlResponse(trace_payload)):
            ip = rotator.get_public_ip(require_warp=True)
            self.assertEqual(ip, "104.28.213.128")

    def test_get_public_ip_rejects_host_direct_ip(self):
        rotator.HOST_DIRECT_IP = "141.11.190.114"
        trace_payload = "ip=141.11.190.114\nwarp=on\n"
        with patch("curl_cffi.requests.get", return_value=FakeCurlResponse(trace_payload)):
            ip = rotator.get_public_ip(require_warp=True)
            self.assertIsNone(ip)

    def test_get_public_ip_uses_custom_outbound_proxy_env(self):
        os.environ["CUSTOM_OUTBOUND_PROXY"] = "socks5://127.0.0.1:41000"
        trace_payload = "ip=104.28.213.127\nwarp=on\n"
        with patch("curl_cffi.requests.get", return_value=FakeCurlResponse(trace_payload)) as mock_get:
            ip = rotator.get_public_ip(require_warp=True)
            self.assertEqual(ip, "104.28.213.127")
            self.assertTrue(mock_get.called)
            _, kwargs = mock_get.call_args
            self.assertEqual(kwargs.get("proxies"), {
                "http": "socks5://127.0.0.1:41000",
                "https": "socks5://127.0.0.1:41000",
            })

    def test_rotation_callbacks_drain_and_close_sessions(self):
        start_called = False
        end_called = False

        def on_start():
            nonlocal start_called
            start_called = True

        def on_end(success, new_ip):
            nonlocal end_called
            end_called = True

        rotator.register_rotation_callbacks(on_start=on_start, on_end=on_end)
        rotator._notify_rotation_start()
        self.assertTrue(start_called)

        rotator._notify_rotation_end(True, "104.28.213.127")
        self.assertTrue(end_called)

    def test_signal_rotation_start_and_done(self):
        with patch.object(server, "_close_all_sessions") as mock_close:
            server.signal_rotation_start()
            self.assertTrue(server._rotation_in_progress.is_set())
            self.assertTrue(mock_close.called)

            mock_close.reset_mock()
            server.signal_rotation_done()
            self.assertFalse(server._rotation_in_progress.is_set())
            self.assertTrue(mock_close.called)


if __name__ == "__main__":
    unittest.main()
