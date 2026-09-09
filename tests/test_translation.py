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


    def test_normalize_content_for_responses(self):
        # Plain text list (as emitted by Oh My Pi and other agents)
        parts = [{"type": "text", "text": "Part 1"}, {"type": "text", "text": "Part 2"}]
        res = server.normalize_content_for_responses(parts, "user")
        self.assertEqual(res, "Part 1\n\nPart 2")

        # Multimodal list
        mm_parts = [
            {"type": "text", "text": "Describe this:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,123"}}
        ]
        res_mm = server.normalize_content_for_responses(mm_parts, "user")
        self.assertEqual(len(res_mm), 2)
        self.assertEqual(res_mm[0], {"type": "input_text", "text": "Describe this:"})
        self.assertEqual(res_mm[1], {"type": "input_image", "image_url": "data:image/png;base64,123"})

    def test_normalize_content_for_chat(self):
        parts = [
            {"type": "input_text", "text": "Describe this:"},
            {"type": "input_image", "image_url": "data:image/png;base64,123"}
        ]
        chat_parts = server.normalize_content_for_chat(parts)
        self.assertEqual(len(chat_parts), 2)
        self.assertEqual(chat_parts[0], {"type": "text", "text": "Describe this:"})
        self.assertEqual(chat_parts[1], {"type": "image_url", "image_url": {"url": "data:image/png;base64,123"}})

    def test_chat_to_responses_payload_with_content_array(self):
        chat_req = {
            "model": "muse-spark-1.3-contributor-free",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<system-reminder>...</system-reminder>"},
                        {"type": "text", "text": "Context replaced..."}
                    ]
                }
            ]
        }
        resp_req = server.chat_to_responses_payload(chat_req)
        self.assertIsInstance(resp_req["input"][0]["content"], str)
        self.assertIn("<system-reminder>", resp_req["input"][0]["content"])
        self.assertIn("Context replaced", resp_req["input"][0]["content"])

    def test_normalize_reasoning_for_responses(self):
        # 1. reasoning_effort string converts to reasoning dict and removes reasoning_effort
        p1 = {"reasoning_effort": "high"}
        res1 = server.normalize_reasoning_for_responses(p1)
        self.assertNotIn("reasoning_effort", res1)
        self.assertEqual(res1["reasoning"], {"effort": "high"})

        # 2. boolean reasoning: True -> {}, False -> dropped
        p2_true = {"reasoning": True}
        res2_true = server.normalize_reasoning_for_responses(p2_true)
        self.assertEqual(res2_true["reasoning"], {})

        p2_false = {"reasoning": False}
        res2_false = server.normalize_reasoning_for_responses(p2_false)
        self.assertNotIn("reasoning", res2_false)

        # 3. string reasoning converts to struct
        p3 = {"reasoning": "medium"}
        res3 = server.normalize_reasoning_for_responses(p3)
        self.assertEqual(res3["reasoning"], {"effort": "medium"})

        # 4. uppercase effort inside struct is normalized
        p4 = {"reasoning": {"effort": "HIGH"}}
        res4 = server.normalize_reasoning_for_responses(p4)
        self.assertEqual(res4["reasoning"], {"effort": "high"})

        # 5. Anthropic thinking blocks mapped to effort tiers
        p5_high = {"thinking": {"type": "enabled", "budget_tokens": 16000}}
        res5_high = server.normalize_reasoning_for_responses(p5_high)
        self.assertNotIn("thinking", res5_high)
        self.assertEqual(res5_high["reasoning"], {"effort": "high"})

        p5_med = {"thinking": {"type": "enabled", "budget_tokens": 8000}}
        res5_med = server.normalize_reasoning_for_responses(p5_med)
        self.assertEqual(res5_med["reasoning"], {"effort": "medium"})

        p5_low = {"thinking": {"type": "enabled", "budget_tokens": 2000}}
        res5_low = server.normalize_reasoning_for_responses(p5_low)
        self.assertEqual(res5_low["reasoning"], {"effort": "low"})

        p5_enabled = {"thinking": {"type": "enabled"}}
        res5_enabled = server.normalize_reasoning_for_responses(p5_enabled)
        self.assertEqual(res5_enabled["reasoning"], {})

    def test_normalize_reasoning_for_chat(self):
        # 1. struct reasoning converted to string reasoning_effort
        p1 = {"reasoning": {"effort": "high"}}
        res1 = server.normalize_reasoning_for_chat(p1)
        self.assertNotIn("reasoning", res1)
        self.assertEqual(res1["reasoning_effort"], "high")

        # 2. boolean True -> 'medium'
        p2 = {"reasoning": True}
        res2 = server.normalize_reasoning_for_chat(p2)
        self.assertNotIn("reasoning", res2)
        self.assertEqual(res2["reasoning_effort"], "medium")

        # 3. boolean False -> dropped
        p3 = {"reasoning": False}
        res3 = server.normalize_reasoning_for_chat(p3)
        self.assertNotIn("reasoning", res3)
        self.assertNotIn("reasoning_effort", res3)

        # 4. Anthropic thinking mapped to reasoning_effort
        p4 = {"thinking": {"type": "enabled", "budget_tokens": 20000}}
        res4 = server.normalize_reasoning_for_chat(p4)
        self.assertNotIn("thinking", res4)
        self.assertEqual(res4["reasoning_effort"], "high")

    def test_chat_to_responses_payload_reasoning(self):
        chat_req = {
            "model": "muse-spark-1.3-contributor-free",
            "messages": [
                {"role": "developer", "content": "You are a test assistant."},
                {"role": "user", "content": "Hello"}
            ],
            "reasoning_effort": "high"
        }
        resp_req = server.chat_to_responses_payload(chat_req)
        self.assertNotIn("reasoning_effort", resp_req)
        self.assertEqual(resp_req["reasoning"], {"effort": "high"})
        self.assertIn("You are a test assistant.", resp_req["instructions"])

    def test_responses_to_chat_payload_reasoning(self):
        resp_req = {
            "model": "nemotron-3-ultra-free",
            "input": "Solve this equation",
            "reasoning": {"effort": "high"}
        }
        chat_req = server.responses_to_chat_payload(resp_req)
        self.assertNotIn("reasoning", chat_req)
        self.assertEqual(chat_req["reasoning_effort"], "high")

    def test_anthropic_to_responses_payload_thinking(self):
        anthropic_req = {
            "model": "muse-spark-1.3-contributor-free",
            "messages": [{"role": "user", "content": "Hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 8000}
        }
        resp_req = server.anthropic_to_responses_payload(anthropic_req)
        self.assertNotIn("thinking", resp_req)
        self.assertEqual(resp_req["reasoning"], {"effort": "medium"})

    def test_responses_to_chat_json_reasoning_content(self):
        resp_json = {
            "id": "resp_reasoning_1",
            "model": "muse-spark-1.3-contributor-free",
            "output": [
                {"type": "reasoning", "summary": "Step 1: Analyzed input. Step 2: Computed result."},
                {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "Answer: 42"}]}
            ]
        }
        chat_json = server.responses_to_chat_json(resp_json, "muse-spark-1.3-contributor-free")
        msg = chat_json["choices"][0]["message"]
        self.assertEqual(msg["content"], "Answer: 42")
        self.assertEqual(msg["reasoning_content"], "Step 1: Analyzed input. Step 2: Computed result.")

    def test_model_usage_sorted_by_total_tokens(self):
        sample_stats = {
            "model-small": {"requests": 5, "prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100, "estimated_cost_usd": 0.001},
            "model-huge": {"requests": 200, "prompt_tokens": 10000, "completion_tokens": 20000, "total_tokens": 30000, "estimated_cost_usd": 0.5},
            "model-mid": {"requests": 20, "prompt_tokens": 500, "completion_tokens": 500, "total_tokens": 1000, "estimated_cost_usd": 0.02},
        }
        sorted_dict = dict(
            sorted(
                sample_stats.items(),
                key=lambda x: (
                    x[1].get("total_tokens", 0),
                    x[1].get("requests", 0),
                    x[1].get("estimated_cost_usd", 0),
                ),
                reverse=True,
            )
        )
        keys = list(sorted_dict.keys())
        self.assertEqual(keys, ["model-huge", "model-mid", "model-small"])


if __name__ == "__main__":
    unittest.main()
