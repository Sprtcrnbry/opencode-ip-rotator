"""End-to-end repro: tool calls across /v1/chat/completions, /v1/messages, /v1/responses + /v1/models caps."""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import server

app = server.app
client = TestClient(app)

TOOL = {"type": "function", "function": {"name": "bash", "description": "run shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}

# --- OpenAI wire format fixtures -------------------------------------------------
NONSTREAM_BODY = {
    "id": "x", "object": "chat.completion", "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "bash", "arguments": "{\"command\":\"ls\"}"}}]},
                 "finish_reason": "tool_calls"}],
}
SSE_CHUNKS = [
    'data: {"id":"x","choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"bash","arguments":"{\\"command\\":"}}]}}]}',
    'data: {"id":"x","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]}}]}',
    'data: {"id":"x","choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    "data: [DONE]",
]

# --- Anthropic wire format fixtures ----------------------------------------------
ANTH_REQ = {
    "model": "big-pickle", "max_tokens": 1024, "stream": False,
    "system": "be terse",
    "tools": [{"name": "bash", "description": "run shell", "input_schema": TOOL["function"]["parameters"]}],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},                      # block-content user msg
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "bash",
                                           "input": {"command": "ls"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "a.txt"}]},
                                     {"type": "text", "text": "now read it"}]},
    ],
}
ANTH_SSE = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"bash","arguments":"{\\"command\\":"}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"ls\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":9,"completion_tokens":5}}',
]

# --- Responses API passthrough fixture (frame-grouped SSE) ------------------------
RESP_SSE = [
    b"event: response.created",
    b'data: {"type":"response.created"}',
    b"",
    b"event: response.output_item.added",
    b'data: {"type":"response.output_item.added","item":{"type":"function_call","name":"bash"}}',
    b"",
    b"event: response.output_text.delta",
    b"data: partial-a",
    b"data: partial-b",
    b"",
    b"event: response.completed",
    b'data: {"type":"response.completed"}',
    b"",
]


class FakeResponse:
    status_code = 200
    headers = {}
    def __init__(self, sse=None, json_body=None):
        self._sse = sse or []
        self._json = json_body

    def iter_lines(self):
        yield from self._sse

    def raise_for_status(self):
        pass

    @property
    def text(self):
        return json.dumps(self._json)

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.last_payload = None

    def post(self, url, headers=None, json=None, timeout=None, stream=False, **kw):
        self.last_url = url
        self.last_payload = json
        return self._resp


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name} {detail}")
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")


# 1. Chat Completions: streaming tool call ---------------------------------------
with patch.object(server, "create_fresh_session", return_value=FakeSession(FakeResponse(sse=[c.encode() for c in SSE_CHUNKS]))), \
     patch.object(server, "record_client_request"):
    r = client.post("/v1/chat/completions", json={"model": "big-pickle", "stream": True, "messages": [{"role": "user", "content": "hi"}], "tools": [TOOL]})
body = r.text
check("chat-stream: tool name streamed", '"bash"' in body)
check("chat-stream: finish_reason tool_calls", '"finish_reason":"tool_calls"' in body)
check("chat-stream: DONE terminator", "data: [DONE]" in body)

# 2. Anthropic Messages: translator role attribution ------------------------------
out = server.anthropic_to_openai(ANTH_REQ)
roles = [(m["role"], bool(m.get("tool_calls")), m.get("tool_call_id")) for m in out["messages"]]
check("anth-translate: roles", roles[1:] == [
    ("user", False, None),
    ("assistant", True, None),
    ("tool", False, "t1"),
    ("user", False, None),
], str(roles))
check("anth-translate: system extracted", out["messages"][0]["role"] == "system")

# 3. Anthropic Messages: non-stream tool_use response -----------------------------
fs = FakeSession(FakeResponse(json_body=NONSTREAM_BODY))
with patch.object(server, "create_fresh_session", return_value=fs), \
     patch.object(server, "record_client_request"):
    r = client.post("/v1/messages", json=dict(ANTH_REQ, stream=False))
up = fs.last_payload["messages"]
check("anth-nonstream: upstream roles preserved", [m["role"] for m in up[1:]] == ["user", "assistant", "tool", "user"], str([m["role"] for m in up]))
resp = r.json()
blocks = resp.get("content", [])
check("anth-nonstream: tool_use block emitted", any(b.get("type") == "tool_use" and b.get("name") == "bash" for b in blocks), str(blocks))
check("anth-nonstream: stop_reason tool_use", resp.get("stop_reason") == "tool_use")
check("anth-nonstream: input parsed", blocks and blocks[-1].get("input") == {"command": "ls"})

# 4. Anthropic Messages: streaming tool call --------------------------------------
fs = FakeSession(FakeResponse(sse=[c.encode() for c in ANTH_SSE]))
with patch.object(server, "create_fresh_session", return_value=fs), \
     patch.object(server, "record_client_request"):
    r = client.post("/v1/messages", json=dict(ANTH_REQ, stream=True))
body = r.text
events = [json.loads(l.split("data:", 1)[1]) for l in body.split("\n") if l.startswith("data:")]
kinds = [e.get("type") for e in events]
check("anth-stream: content_block_start tool_use",
      any(e.get("type") == "content_block_start" and e.get("content_block", {}).get("type") == "tool_use" for e in events), str(kinds))
check("anth-stream: input_json_delta present", any(e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "input_json_delta" for e in events))
check("anth-stream: stop_reason tool_use", any(e.get("type") == "message_delta" and e.get("delta", {}).get("stop_reason") == "tool_use" for e in events))

# 5. Responses API: SSE frames preserved verbatim ---------------------------------
fs = FakeSession(FakeResponse(sse=RESP_SSE))
with patch.object(server, "create_fresh_session", return_value=fs), \
     patch.object(server, "record_client_request"):
    r = client.post("/v1/responses", json={"model": "big-pickle", "stream": True, "input": "list files", "tools": [TOOL]})
raw = r.content
frames = [f for f in raw.split(b"\n\n") if f.strip()]
check("responses: frame count", len(frames) == 4, f"{len(frames)} frames")
f1 = frames[1].split(b"\n")
check("responses: event+data grouped in ONE frame",
      f1[0].startswith(b"event: response.output_item.added") and len(f1) == 2 and b"function_call" in f1[1],
      str(frames[1][:80]))
multi = frames[2].split(b"\n")
check("responses: multi-line data kept together", len(multi) == 3 and multi[0].startswith(b"event:") and multi[1] == b"data: partial-a", str(multi))
check("responses: completed frame forwarded", b"response.completed" in raw)
check("responses: no synthetic [DONE]", b"[DONE]" not in raw)

# 6. Models endpoint exposes capability metadata ----------------------------------
r = client.get("/v1/models")
models = {m["id"]: m for m in r.json()["data"]}
m = models.get("deepseek-v4-flash-free", {})
check("models: context_length present", m.get("context_length", 0) > 0, f"{m.get('context_length')}")
check("models: max_completion_tokens present", m.get("max_completion_tokens", 0) > 0, f"{m.get('max_completion_tokens')}")
check("models: reasoning flag + options", isinstance(m.get("reasoning"), bool) and "reasoning_options" in m)
check("models: capabilities object", all(k in m.get("capabilities", {}) for k in ("tools", "reasoning")))
check("models: limit object", m.get("limit", {}).get("context", 0) > 0)

# 7. Offline fallback still enriches defaults -------------------------------------
with patch.object(server, "urlopen", side_effect=OSError("offline")):
    fb = server.fetch_models_from_server()
check("models-fallback: returns enriched defaults", bool(fb) and all("context_tokens" in x and "reasoning" in x for x in fb))

print("\nALL CHECKS PASSED")
