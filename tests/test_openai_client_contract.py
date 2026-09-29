from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, cast

import httpx
import pytest
from openai import (
    APIError,
    AsyncOpenAI,
    AsyncStream,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OpenAI,
)
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import ResponseCompletedEvent, ResponseStreamEvent


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(filter(None, (_flatten_text(item) for item in value)))
    if isinstance(value, dict):
        if value.get("type") in {"input_text", "output_text", "text"}:
            return str(value.get("text") or "")
        return " ".join(
            filter(
                None, (_flatten_text(value.get(key)) for key in ("content", "input"))
            )
        )
    return ""


class _FakeCodexHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    counter = 0
    last_payload: ClassVar[dict[str, Any]] = {}
    last_headers: ClassVar[dict[str, str]] = {}

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_GET(self) -> None:
        if self.path.startswith("/backend-api/codex/models"):
            self._json(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-luna",
                            "supported_in_api": True,
                            "visibility": "list",
                        },
                        {
                            "slug": "hidden-model",
                            "supported_in_api": True,
                            "visibility": "hidden",
                        },
                    ]
                }
            )
            return
        self._proxy_response()

    def do_POST(self) -> None:
        if self.path == "/backend-api/codex/responses":
            size = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(size))
            type(self).last_payload = payload
            type(self).last_headers = dict(self.headers.items())
            self._responses_stream(payload)
            return
        if self.path == "/backend-api/transcribe":
            size = int(self.headers.get("content-length", "0"))
            self.rfile.read(size)
            self._json({"text": "transcribed by fake Codex"})
            return
        self._proxy_response()

    def do_DELETE(self) -> None:
        self._proxy_response()

    def _responses_stream(self, payload: dict[str, Any]) -> None:
        type(self).counter += 1
        number = type(self).counter
        response_id = f"resp_contract_{number}"
        created = time.time()
        tools = payload.get("tools") or []
        is_image = any(tool.get("type") == "image_generation" for tool in tools)
        is_tool = any(tool.get("type") == "function" for tool in tools)

        if is_image:
            item = {
                "id": f"ig_contract_{number}",
                "type": "image_generation_call",
                "status": "completed",
                "result": base64.b64encode(b"\x89PNG\r\n\x1a\ncontract").decode(),
                "revised_prompt": "revised contract image",
            }
        elif is_tool:
            item = {
                "id": f"fc_contract_{number}",
                "type": "function_call",
                "call_id": f"call_contract_{number}",
                "name": "lookup_weather",
                "arguments": '{"city":"Tokyo"}',
                "status": "completed",
            }
        else:
            text = "fake contract: " + _flatten_text(payload.get("input"))
            item = {
                "id": f"msg_contract_{number}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        response = {
            "id": response_id,
            "object": "response",
            "created_at": created,
            "status": "completed",
            "model": payload.get("model"),
            "output": [item],
            "parallel_tool_calls": True,
            "tool_choice": payload.get("tool_choice", "auto"),
            "tools": tools,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
                "input_tokens_details": {"cached_tokens": 2},
                "output_tokens_details": {"reasoning_tokens": 1},
            },
        }
        if "CONTRACT-REASONING" in _flatten_text(payload.get("input")):
            response["output"].insert(
                0,
                {
                    "type": "reasoning",
                    "id": "rs_contract",
                    "summary": [],
                    "encrypted_content": "opaque-contract-cipher",
                },
            )
        created_response = {**response, "status": "in_progress", "output": []}
        events: list[dict[str, Any]] = [
            {
                "type": "response.created",
                "sequence_number": 0,
                "response": created_response,
            }
        ]
        if is_tool:
            events.extend(
                [
                    {
                        "type": "response.output_item.added",
                        "sequence_number": 1,
                        "output_index": 0,
                        "item": {**item, "arguments": "", "status": "in_progress"},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": 2,
                        "output_index": 0,
                        "item_id": item["id"],
                        "delta": '{"city":"Tokyo"}',
                    },
                ]
            )
        elif not is_image:
            message_text = cast(dict[str, Any], cast(list[Any], item["content"])[0])[
                "text"
            ]
            events.append(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": item["id"],
                    "delta": message_text,
                    "logprobs": [],
                }
            )
        events.extend(
            [
                {
                    "type": "response.output_item.done",
                    "sequence_number": 3,
                    "output_index": 0,
                    "item": item,
                },
                {
                    "type": "response.completed",
                    "sequence_number": 4,
                    "response": response,
                },
            ]
        )
        if "CONTRACT-TRUNCATE" in _flatten_text(payload.get("input")):
            events = [e for e in events if e["type"] != "response.completed"]
        chunks = (
            "".join(
                f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                for event in events
            )
            + "data: [DONE]\n\n"
        )
        encoded = chunks.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _proxy_response(self) -> None:
        self._json(
            {
                "object": "list",
                "data": [],
                "has_more": False,
                "request_method": self.command,
                "request_path": self.path,
            },
            headers={"x-upstream-request-id": "contract-upstream"},
        )

    def _json(
        self,
        value: Any,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture(scope="module")
def fake_codex_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCodexHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = cast(tuple[str, int], server.server_address)
    host, port = address
    try:
        yield f"http://{host}:{port}/backend-api/codex"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def go_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    binary = tmp_path_factory.mktemp("go-bin") / "openai-api-server-via-codex"
    subprocess.run(
        ["go", "build", "-o", str(binary), "./cmd/openai-api-server-via-codex"],
        check=True,
    )
    return binary


@pytest.fixture(scope="module")
def runtime_server(
    fake_codex_url: str,
    go_binary: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    runtime = "go"
    work = tmp_path_factory.mktemp("contract-go")
    auth_json = work / "auth.json"
    auth_json.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "header.payload.signature",
                    "account_id": "contract-account",
                },
            }
        )
    )
    port = _free_port()
    command = [
        str(go_binary),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--auth-json",
        str(auth_json),
        "--backend-base-url",
        fake_codex_url,
        "--max-stored-items",
        "100",
        "--api-key",
        "contract-server-key",
    ]
    log_path = work / "server.log"
    # An unread PIPE fills during larger contract suites and blocks Go logs.
    with log_path.open("w", encoding="utf-8") as server_log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).parents[1],
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = log_path.read_text(encoding="utf-8", errors="replace")
                raise AssertionError(f"{runtime} server exited early:\n{output}")
            try:
                if httpx.get(f"{base_url}/healthz", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise AssertionError(f"{runtime} server did not become healthy")
        yield runtime, base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def contract_client(runtime_server: tuple[str, str]) -> AsyncOpenAI:
    _, base_url = runtime_server
    return AsyncOpenAI(api_key="contract-server-key", base_url=f"{base_url}/v1")


@pytest.mark.parametrize("streaming", [False, True])
async def test_contract_chat_cache_key_and_usage(
    contract_client: AsyncOpenAI,
    streaming: bool,
) -> None:
    async with contract_client as client:
        result = await client.chat.completions.create(
            model="gpt-5.6-luna",
            messages=[{"role": "user", "content": "cache metrics"}],
            prompt_cache_key="sdk-cache-session",
            stream=streaming,
            stream_options={"include_usage": True} if streaming else None,
        )
        if isinstance(result, AsyncStream):
            chunks = [
                chunk async for chunk in cast(AsyncStream[ChatCompletionChunk], result)
            ]
            usage = next(chunk.usage for chunk in chunks if chunk.usage is not None)
        else:
            usage = result.usage
        assert usage is not None
        assert usage.prompt_tokens_details is not None
        assert usage.completion_tokens_details is not None
        assert usage.prompt_tokens_details.cached_tokens == 2
        assert usage.completion_tokens_details.reasoning_tokens == 1
        assert usage.total_tokens == 8
        assert _FakeCodexHandler.last_payload["prompt_cache_key"] == "sdk-cache-session"


def test_contract_sync_responses_cache_key_and_usage(
    runtime_server: tuple[str, str],
) -> None:
    _, base_url = runtime_server
    with OpenAI(api_key="contract-server-key", base_url=f"{base_url}/v1") as client:
        first = client.responses.create(model="gpt-5.6-luna", input="cache first")
        key = _FakeCodexHandler.last_payload["prompt_cache_key"]
        headers = {k.lower(): v for k, v in _FakeCodexHandler.last_headers.items()}
        assert key.startswith("auto_")
        assert first.usage is not None
        assert first.usage.input_tokens_details.cached_tokens == 2
        client.responses.create(
            model="gpt-5.6-luna", previous_response_id=first.id, input="followup"
        )
        assert _FakeCodexHandler.last_payload["prompt_cache_key"] == key
        next_headers = {k.lower(): v for k, v in _FakeCodexHandler.last_headers.items()}
        assert headers["session_id"] == next_headers["session_id"] == key
        assert headers["x-client-request-id"] != next_headers["x-client-request-id"]
        client.responses.create(
            model="gpt-5.6-luna", input="explicit", prompt_cache_key="explicit-response"
        )
        assert _FakeCodexHandler.last_payload["prompt_cache_key"] == "explicit-response"


@pytest.mark.parametrize("endpoint", ["responses", "chat"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_contract_truncated_stream_is_not_success(
    contract_client: AsyncOpenAI,
    endpoint: str,
    streaming: bool,
) -> None:
    async with contract_client.with_options(max_retries=0) as client:
        if not streaming:
            with pytest.raises(InternalServerError, match="before a terminal response"):
                if endpoint == "responses":
                    await client.responses.create(
                        model="gpt-5.6-luna", input="CONTRACT-TRUNCATE"
                    )
                else:
                    await client.chat.completions.create(
                        model="gpt-5.6-luna",
                        messages=[{"role": "user", "content": "CONTRACT-TRUNCATE"}],
                    )
        elif endpoint == "responses":
            stream = await client.responses.create(
                model="gpt-5.6-luna", input="CONTRACT-TRUNCATE", stream=True
            )
            events = [event async for event in stream]
            assert any(
                e.type == "error"
                and "before a terminal response" in str(e.model_dump().get("message"))
                for e in events
            )
            assert not any(e.type == "response.completed" for e in events)
        else:
            stream = await client.chat.completions.create(
                model="gpt-5.6-luna",
                messages=[{"role": "user", "content": "CONTRACT-TRUNCATE"}],
                stream=True,
            )
            with pytest.raises(APIError, match="before a terminal response"):
                async for chunk in stream:
                    assert not any(choice.finish_reason for choice in chunk.choices)


@pytest.mark.parametrize("streaming", [False, True])
async def test_contract_previous_response_restores_encrypted_reasoning(
    contract_client: AsyncOpenAI,
    streaming: bool,
) -> None:
    async with contract_client as client:
        result = await client.responses.create(
            model="gpt-5.6-luna", input="CONTRACT-REASONING", stream=streaming
        )
        if isinstance(result, AsyncStream):
            events = [
                event async for event in cast(AsyncStream[ResponseStreamEvent], result)
            ]
            first = next(
                event.response
                for event in events
                if isinstance(event, ResponseCompletedEvent)
            )
        else:
            first = result
        await client.responses.create(
            model="gpt-5.6-luna", input="next", previous_response_id=first.id
        )
        items = _FakeCodexHandler.last_payload["input"]
        assert items[1]["type"] == "reasoning"
        assert items[1]["id"] == "rs_contract"
        assert items[1]["encrypted_content"] == "opaque-contract-cipher"
        assert items[2]["role"] == "assistant"
        assert items[2]["phase"] == "final_answer"
        assert items[2]["type"] == "message"
        assert items[2]["status"] == "completed"
        assert items[2]["id"].startswith("msg_contract_")
        assert items[-1]["content"] == "next"


async def test_contract_health_and_models(runtime_server: tuple[str, str]) -> None:
    runtime, base_url = runtime_server
    async with httpx.AsyncClient(base_url=base_url) as direct:
        health = await direct.get("/healthz")
        invalid_health_method = await direct.post("/healthz")
        outside_v1 = await direct.get("/outside-v1")
        unauthorized = await direct.get("/v1/models")
        unauthorized_v1_root = await direct.get("/v1")
    assert health.json() == {"status": "ok"}, runtime
    assert invalid_health_method.status_code == 405, runtime
    assert outside_v1.status_code == 404, runtime
    assert unauthorized.status_code == 401, runtime
    assert unauthorized.json()["error"]["code"] == "invalid_api_key", runtime
    assert unauthorized_v1_root.status_code == 401, runtime
    async with AsyncOpenAI(
        api_key="contract-server-key", base_url=f"{base_url}/v1"
    ) as client:
        models = await client.models.list()
    assert [model.id for model in models.data] == ["gpt-5.6-luna"], runtime

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": "bearer contract-server-key"},
    ) as direct:
        root = await direct.get("/v1")
        root_with_slash = await direct.get("/v1/")
    assert root.status_code == 307, runtime
    assert root.headers["location"].endswith("/v1/"), runtime
    assert root_with_slash.status_code == 200, runtime
    assert root_with_slash.json()["request_path"].endswith("/backend-api/codex/"), (
        runtime
    )


async def test_contract_responses_lifecycle_and_streaming(
    runtime_server: tuple[str, str], contract_client: AsyncOpenAI
) -> None:
    runtime, _ = runtime_server
    created = await contract_client.responses.create(
        model="gpt-5.6-luna",
        input=[
            {"role": "user", "content": "response lifecycle marker"},
            {"role": "user", "content": "second input marker"},
        ],
    )
    assert "response lifecycle marker" in created.output_text, runtime
    assert "second input marker" in created.output_text, runtime

    retrieved = await contract_client.responses.retrieve(created.id)
    assert retrieved.output_text == created.output_text, runtime

    with pytest.raises(ConflictError):
        await contract_client.responses.cancel(created.id)

    input_items = await contract_client.responses.input_items.list(created.id, limit=1)
    assert input_items.has_more is True, runtime
    assert input_items.data[0].model_dump(mode="json", exclude_none=True) == {
        "id": "input_0",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "response lifecycle marker"}],
    }, runtime
    next_input_items = await contract_client.responses.input_items.list(
        created.id, after="input_0", limit=2
    )
    assert next_input_items.has_more is False, runtime
    assert [item.id for item in next_input_items.data] == ["input_1"], runtime

    counted = await contract_client.responses.input_tokens.count(
        model="gpt-5.6-luna", input="count this input"
    )
    assert counted.input_tokens > 0, runtime

    continued = await contract_client.responses.create(
        model="gpt-5.6-luna",
        previous_response_id=created.id,
        input="continued marker",
    )
    assert continued.previous_response_id == created.id, runtime
    assert "response lifecycle marker" in continued.output_text, runtime
    assert "continued marker" in continued.output_text, runtime

    stream = await contract_client.responses.create(
        model="gpt-5.6-luna", input="response stream marker", stream=True
    )
    event_types: list[str] = []
    deltas: list[str] = []
    streamed_id = ""
    async for event in stream:
        event_types.append(event.type)
        if event.type == "response.output_text.delta":
            deltas.append(cast(Any, event).delta)
        if event.type == "response.completed":
            streamed_id = cast(Any, event).response.id
    assert "response.completed" in event_types, runtime
    assert "response stream marker" in "".join(deltas), runtime

    replay = await contract_client.responses.retrieve(streamed_id, stream=True)
    replay_types = [event.type async for event in replay]
    assert replay_types[-1] == "response.completed", runtime

    await contract_client.responses.delete(created.id)
    with pytest.raises(NotFoundError):
        await contract_client.responses.retrieve(created.id)


async def test_contract_chat_lifecycle_streaming_and_tools(
    runtime_server: tuple[str, str], contract_client: AsyncOpenAI
) -> None:
    runtime, _ = runtime_server
    completion = await contract_client.chat.completions.create(
        model="gpt-5.6-luna",
        messages=[{"role": "user", "content": "chat lifecycle marker"}],
        n=2,
        store=True,
        metadata={"suite": "contract"},
    )
    assert "chat lifecycle marker" in (completion.choices[0].message.content or ""), (
        runtime
    )

    retrieved = await contract_client.chat.completions.retrieve(completion.id)
    assert retrieved.id == completion.id, runtime
    listed = await contract_client.chat.completions.list(metadata={"suite": "contract"})
    assert completion.id in [item.id for item in listed.data], runtime
    updated = await contract_client.chat.completions.update(
        completion.id, metadata={"suite": "updated"}
    )
    assert updated.model_dump().get("metadata") == {"suite": "updated"}, runtime
    messages = await contract_client.chat.completions.messages.list(completion.id)
    assert messages.data[0].role == "assistant", runtime
    descending_messages = await contract_client.chat.completions.messages.list(
        completion.id, order="desc"
    )
    assert [message.id for message in descending_messages.data] == [
        f"{completion.id}_msg_1",
        f"{completion.id}_msg_0",
    ], runtime
    remaining_messages = await contract_client.chat.completions.messages.list(
        completion.id, after=f"{completion.id}_msg_0", limit=2
    )
    assert remaining_messages.has_more is False, runtime
    assert [message.id for message in remaining_messages.data] == [
        f"{completion.id}_msg_1"
    ], runtime

    stream = await contract_client.chat.completions.create(
        model="gpt-5.6-luna",
        messages=[{"role": "user", "content": "chat stream marker"}],
        n=2,
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks = [chunk async for chunk in stream]
    streamed_by_choice = {
        index: "".join(
            choice.delta.content or ""
            for chunk in chunks
            for choice in chunk.choices
            if choice.index == index
        )
        for index in (0, 1)
    }
    assert all("chat stream marker" in text for text in streamed_by_choice.values()), (
        runtime
    )
    assert any(chunk.usage is not None for chunk in chunks), runtime

    tool = await contract_client.chat.completions.create(
        model="gpt-5.6-luna",
        messages=[{"role": "user", "content": "use the weather tool"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup_weather",
                    "description": "Look up weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
        tool_choice={"type": "function", "function": {"name": "lookup_weather"}},
    )
    tool_calls = tool.choices[0].message.tool_calls
    assert tool_calls is not None, runtime
    assert tool_calls[0].model_dump()["function"]["name"] == "lookup_weather", runtime

    deleted = await contract_client.chat.completions.delete(completion.id)
    assert deleted.deleted is True, runtime


async def test_contract_images_audio_and_unknown_proxy(
    runtime_server: tuple[str, str], contract_client: AsyncOpenAI
) -> None:
    runtime, base_url = runtime_server
    image = await contract_client.images.generate(
        model="gpt-5.6-luna", prompt="draw a contract", size="256x256"
    )
    assert image.data is not None and image.data[0].b64_json is not None, runtime
    assert base64.b64decode(image.data[0].b64_json).startswith(b"\x89PNG"), runtime

    transcription = await contract_client.audio.transcriptions.create(
        model="gpt-4o-transcribe",
        file=("sample.wav", b"RIFF-contract", "audio/wav"),
    )
    assert transcription.text == "transcribed by fake Codex", runtime

    async with httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": "Bearer contract-server-key"}
    ) as direct:
        proxied = await direct.get("/v1/batches?limit=3")
        encoded_delimiters = await direct.get(
            "/v1/files/report%3Fformat%23section?limit=1"
        )
        literal_percent = await direct.get("/v1/files/100%25done")
        encoded_traversal = await direct.get("/v1/files/%2e%2e/auth")
    assert proxied.json()["object"] == "list", runtime
    assert proxied.headers["x-openai-via-codex-proxy"] == "codex-http", runtime
    assert encoded_delimiters.json()["request_path"].endswith(
        "/backend-api/codex/files/report%3Fformat%23section?limit=1"
    ), runtime
    assert literal_percent.json()["request_path"].endswith(
        "/backend-api/codex/files/100%25done"
    ), runtime
    assert encoded_traversal.status_code == 400, runtime
