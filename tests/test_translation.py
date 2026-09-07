import unittest
import json
import server


class TranslationPayloadTests(unittest.TestCase):
    def test_is_responses_model(self):
        self.assertTrue(server.is_responses_model("muse-spark-1.3-contributor-free"))
        self.assertTrue(server.is_responses_model("muse-spark-1.2"))
        self.assertTrue(server.is_responses_model("MUSE-SPARK-1.3"))
        self.assertFalse(server.is_responses_model("nemotron-3-ultra-free"))
        self.assertFalse(server.is_responses_model("mimo-v2.5-free"))

    def test_chat_to_responses_payload_basic(self):
        chat_req = {
            "model": "muse-spark-1.3-contributor-free",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello world"},
            ],
            "max_completion_tokens": 1024,
            "temperature": 0.7,
            "stream": True,
        }
        resp_req = server.chat_to_responses_payload(chat_req)
        self.assertEqual(resp_req["model"], "muse-spark-1.3-contributor-free")
        self.assertEqual(resp_req["instructions"], "You are a helpful assistant.")
        self.assertEqual(resp_req["max_output_tokens"], 1024)
        self.assertEqual(resp_req["temperature"], 0.7)
        self.assertTrue(resp_req["stream"])
        self.assertEqual(len(resp_req["input"]), 1)
        self.assertEqual(resp_req["input"][0], {"role": "user", "content": "Hello world"})

    def test_chat_to_responses_payload_tools_and_tool_calls(self):
        chat_req = {
            "model": "muse-spark-1.3-contributor-free",
            "messages": [
                {"role": "user", "content": "Check weather in Tokyo"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_tokyo_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": "{\"location\": \"Tokyo\"}"
                            }
                        }
                    ]
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_tokyo_1",
                    "content": "{\"temp\": 22, \"condition\": \"Sunny\"}"
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"location": {"type": "string"}},
                            "required": ["location"]
                        }
                    }
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}}
        }
        resp_req = server.chat_to_responses_payload(chat_req)
        self.assertEqual(len(resp_req["tools"]), 1)
        self.assertEqual(resp_req["tools"][0]["name"], "get_weather")
        self.assertEqual(resp_req["tools"][0]["type"], "function")
        self.assertEqual(resp_req["tool_choice"], {"type": "function", "name": "get_weather"})

        items = resp_req["input"]
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0], {"role": "user", "content": "Check weather in Tokyo"})
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[1]["call_id"], "call_tokyo_1")
        self.assertEqual(items[1]["name"], "get_weather")
        self.assertEqual(items[2]["type"], "function_call_output")
        self.assertEqual(items[2]["call_id"], "call_tokyo_1")
        self.assertEqual(items[2]["output"], "{\"temp\": 22, \"condition\": \"Sunny\"}")

    def test_responses_to_chat_payload_basic(self):
        resp_req = {
            "model": "nemotron-3-ultra-free",
            "instructions": "Be concise.",
            "input": "Hi there",
            "max_output_tokens": 512,
            "stream": False
        }
        chat_req = server.responses_to_chat_payload(resp_req)
        self.assertEqual(chat_req["model"], "nemotron-3-ultra-free")
        self.assertEqual(chat_req["max_tokens"], 512)
        self.assertFalse(chat_req["stream"])
        self.assertEqual(len(chat_req["messages"]), 2)
        self.assertEqual(chat_req["messages"][0], {"role": "system", "content": "Be concise."})
        self.assertEqual(chat_req["messages"][1], {"role": "user", "content": "Hi there"})

    def test_responses_to_chat_payload_tool_calls(self):
        resp_req = {
            "model": "nemotron-3-ultra-free",
            "input": [
                {"role": "user", "content": "Run bash ls"},
                {"type": "function_call", "call_id": "call_1", "name": "bash", "arguments": "{\"cmd\": \"ls\"}"},
                {"type": "function_call_output", "call_id": "call_1", "output": "file1.txt\nfile2.txt"}
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "bash",
                    "description": "Run bash",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}
                }
            ]
        }
        chat_req = server.responses_to_chat_payload(resp_req)
        self.assertEqual(len(chat_req["tools"]), 1)
        self.assertEqual(chat_req["tools"][0]["function"]["name"], "bash")
        msgs = chat_req["messages"]
        self.assertEqual(len(msgs), 3)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(msgs[1]["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(msgs[2]["role"], "tool")
        self.assertEqual(msgs[2]["tool_call_id"], "call_1")
        self.assertEqual(msgs[2]["content"], "file1.txt\nfile2.txt")

    def test_responses_to_chat_json(self):
        resp_json = {
            "id": "resp_test123",
            "model": "muse-spark-1.3-contributor-free",
            "created_at": 1700000000,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello world!"}]
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17}
        }
        chat_json = server.responses_to_chat_json(resp_json, "muse-spark-1.3-contributor-free")
        self.assertEqual(chat_json["id"], "resp_test123")
        self.assertEqual(chat_json["object"], "chat.completion")
        self.assertEqual(chat_json["choices"][0]["message"]["content"], "Hello world!")
        self.assertEqual(chat_json["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chat_json["usage"]["prompt_tokens"], 12)
        self.assertEqual(chat_json["usage"]["completion_tokens"], 5)

    def test_responses_to_chat_json_with_tool_call(self):
        resp_json = {
            "id": "resp_test_tc",
            "model": "muse-spark-1.3-contributor-free",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "read_file",
                    "arguments": "{\"path\": \"/etc/hosts\"}"
                }
            ],
            "usage": {"input_tokens": 20, "output_tokens": 10}
        }
        chat_json = server.responses_to_chat_json(resp_json, "muse-spark-1.3-contributor-free")
        msg = chat_json["choices"][0]["message"]
        self.assertIn("tool_calls", msg)
        self.assertEqual(msg["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(chat_json["choices"][0]["finish_reason"], "tool_calls")

    def test_chat_to_responses_json(self):
        chat_json = {
            "id": "chatcmpl_abc",
            "model": "nemotron-3-ultra-free",
            "created": 1700000000,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Hi from completions!",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "test_fn", "arguments": "{}"}
                            }
                        ]
                    },
                    "finish_reason": "tool_calls"
                }
            ],
            "usage": {"prompt_tokens": 8, "completion_tokens": 14, "total_tokens": 22}
        }
        resp_json = server.chat_to_responses_json(chat_json, "nemotron-3-ultra-free")
        self.assertEqual(resp_json["id"], "chatcmpl_abc")
        self.assertEqual(resp_json["object"], "response")
        output = resp_json["output"]
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["type"], "message")
        self.assertEqual(output[0]["content"][0]["text"], "Hi from completions!")
        self.assertEqual(output[1]["type"], "function_call")
        self.assertEqual(output[1]["call_id"], "call_123")
        self.assertEqual(resp_json["usage"]["input_tokens"], 8)
        self.assertEqual(resp_json["usage"]["output_tokens"], 14)


if __name__ == "__main__":
    unittest.main()
