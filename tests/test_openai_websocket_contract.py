from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from openai import AsyncOpenAI, NotFoundError

from tests.contract_helpers import flatten_text


@pytest.fixture
def websocket_runtime(
    go_binary: Path,
    tmp_path: Path,
) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import ServerConnection, serve

    from tests.websocket_helpers import spawned_server

    received: list[dict[str, Any]] = []
    failures: list[str] = []

    def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        assert connection.request.headers["Authorization"] == "Bearer fake-ws-token"
        assert connection.request.headers["ChatGPT-Account-ID"] == "fake-ws-account"
        sequence = 0

        def emit(event: dict[str, Any]) -> None:
            nonlocal sequence
            connection.send(json.dumps({"sequence_number": sequence, **event}))
            sequence += 1

        def response(rid: str, status: str = "completed") -> dict[str, Any]:
            return {
                "id": rid,
                "object": "response",
                "created_at": 0,
                "model": "gpt-6-astra",
                "status": status,
                "output": [],
            }

        def finish(rid: str, text: str, call: bool = False) -> None:
            emit({"type": "response.created", "response": response(rid, "in_progress")})
            if call:
                emit(
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {
                            "id": "fc_async",
                            "type": "function_call",
                            "call_id": "call_async",
                            "name": "lookup",
                            "arguments": '{"key":"demo"}',
                            "async": True,
                            "status": "completed",
                            "future_field": "preserved",
                        },
                    }
                )
            emit(
                {
                    "type": "response.output_item.done",
                    "output_index": int(call),
                    "item": {
                        "id": "msg_" + rid,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "phase": "final_answer",
                        "content": [
                            {"type": "output_text", "text": text, "annotations": []}
                        ],
                    },
                }
            )
            # Codex may leave final response.output empty; consumers must collect
            # output_item.done. The proxy must not synthesize response contents.
            emit({"type": "response.completed", "response": response(rid)})

        try:
            for raw in connection:
                event = json.loads(raw)
                received.append(event)
                if "stream_id" in event:
                    emit(
                        {
                            "type": "error",
                            "status": 400,
                            "error": {
                                "type": "invalid_request_error",
                                "message": "Unsupported parameter: stream_id",
                            },
                        }
                    )
                    continue
                if event["type"] == "response.steer":
                    assert event["previous_response_id"] == "resp_steering"
                    assert event["input"] == "Reply exactly STEER_OK"
                    emit(
                        {
                            "type": "response.steer.accepted",
                            "steer": {
                                "id": "steer_1",
                                "previous_response_id": "resp_steering",
                            },
                        }
                    )
                    emit(
                        {
                            "type": "response.incomplete",
                            "response": {
                                **response("resp_steering", "incomplete"),
                                "incomplete_details": {"reason": "steered"},
                            },
                        }
                    )
                    finish("resp_successor", "STEER_OK")
                    continue
                assert event["type"] == "response.create"
                assert event["store"] is False and "stream" not in event
                if flatten_text(event["input"]) == "steer-start":
                    emit(
                        {
                            "type": "response.created",
                            "response": response("resp_steering", "in_progress"),
                        }
                    )
                elif event.get("previous_response_id") == "resp_async":
                    assert event["input"] == [{"role": "user", "content": "pending"}]
                    finish("resp_pending", "PENDING_OK")
                elif event.get("previous_response_id") == "resp_pending":
                    assert event["input"][0]["call_id"] == "call_async"
                    assert event["input"][0]["output"] == "VALUE_OK"
                    finish("resp_result", "VALUE_OK")
                elif event.get("tools"):
                    assert event["tools"][0]["async"] is True
                    finish("resp_async", "INDEPENDENT_OK", call=True)
                else:
                    finish("resp_basic", "WS_OK")
        except ConnectionClosed:
            pass
        except Exception as error:
            failures.append(str(error))
            raise

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "fake-ws-token",
                    "account_id": "fake-ws-account",
                },
            }
        )
    )
    with serve(handler, "127.0.0.1", 0) as backend:
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        port = backend.socket.getsockname()[1]
        try:
            with spawned_server(
                go_binary,
                tmp_path,
                backend_url=f"http://127.0.0.1:{port}",
                auth_json=auth,
            ) as base:
                yield base, received
        finally:
            backend.shutdown()
            thread.join(timeout=5)
    assert not failures, failures


async def test_contract_websocket_async_tools_and_upstream_context(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from openai.types.responses import ToolParam

    from tests.websocket_helpers import (
        API_KEY,
        ASYNC_TOOL,
        MODEL,
        output_text,
        receive_terminal,
    )

    base, received = websocket_runtime
    async with (
        AsyncOpenAI(api_key=API_KEY, base_url=base + "/v1") as client,
        client.responses.connect(max_retries=0) as connection,
    ):
        await connection.response.create(
            model=MODEL, input="initial", tools=[cast(ToolParam, ASYNC_TOOL)]
        )
        first, items, _ = await receive_terminal(connection)
        call = next(item for item in items if item["type"] == "function_call")
        assert call["async"] is True and call["future_field"] == "preserved"
        assert output_text(items) == "INDEPENDENT_OK"
        await connection.response.create(
            model=MODEL, input="pending", previous_response_id=first["id"]
        )
        pending, items, _ = await receive_terminal(connection)
        assert output_text(items) == "PENDING_OK"
        await connection.response.create(
            model=MODEL,
            previous_response_id=pending["id"],
            input=[
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": "VALUE_OK",
                }
            ],
        )
        result, items, _ = await receive_terminal(connection)
        assert result["status"] == "completed" and output_text(items) == "VALUE_OK"
        with pytest.raises(NotFoundError):
            await client.responses.retrieve(first["id"])
    assert len(received) == 3


async def test_contract_websocket_steering(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from tests.websocket_helpers import API_KEY, MODEL, output_text, receive_terminal

    base, _ = websocket_runtime
    async with (
        AsyncOpenAI(api_key=API_KEY, base_url=base + "/v1") as client,
        client.responses.connect(max_retries=0) as connection,
    ):
        await connection.response.create(model=MODEL, input="steer-start")
        created = await connection.recv()
        assert created.type == "response.created"
        await connection.response.steer(
            previous_response_id=created.model_dump()["response"]["id"],
            input="Reply exactly STEER_OK",
        )
        interrupted, _, types = await receive_terminal(connection)
        assert "response.steer.accepted" in types
        assert interrupted["incomplete_details"]["reason"] == "steered"
        successor, items, _ = await receive_terminal(connection)
        assert successor["id"] != interrupted["id"]
        assert successor["status"] == "completed" and output_text(items) == "STEER_OK"


def test_contract_websocket_sync_and_background_rejection(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from openai import BadRequestError, OpenAI

    from tests.websocket_helpers import API_KEY, MODEL

    base, received = websocket_runtime
    with OpenAI(api_key=API_KEY, base_url=base + "/v1") as client:
        with pytest.raises(BadRequestError) as error:
            client.responses.create(model=MODEL, input="hello", background=True)
        assert error.value.code == "unsupported_parameter"
        assert error.value.param == "background"
        with client.responses.connect(max_retries=0) as connection:
            connection.response.create(model=MODEL, input="hello", background=True)
            rejected = connection.recv()
            assert (
                rejected.type == "error"
                and rejected.model_dump()["error"]["code"] == "unsupported_parameter"
            )
            connection.response.create(model=MODEL, input="hello", background=False)
            text = ""
            completed = False
            for event in connection:
                data = event.model_dump(by_alias=True)
                if (
                    data["type"] == "response.output_item.done"
                    and data["item"]["type"] == "message"
                ):
                    text += data["item"]["content"][0]["text"]
                if event.type == "response.completed":
                    assert text == "WS_OK"
                    completed = True
                    break
            assert completed
    assert len(received) == 1


async def test_contract_websocket_upstream_named_stream_error(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from tests.websocket_helpers import API_KEY, MODEL

    base, received = websocket_runtime
    async with (
        AsyncOpenAI(api_key=API_KEY, base_url=base + "/v1") as client,
        client.responses.connect(max_retries=0) as connection,
    ):
        await connection.response.create(model=MODEL, stream_id="alpha", input="hello")
        event = (await connection.recv()).model_dump(by_alias=True, exclude_none=True)
        assert event["type"] == "error" and event["status"] == 400
        assert event["error"] == {
            "type": "invalid_request_error",
            "message": "Unsupported parameter: stream_id",
        }
    assert received[0]["stream_id"] == "alpha"


def test_contract_saturated_connections_return_sdk_error(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from contextlib import ExitStack

    from openai import InternalServerError, OpenAI

    from tests.websocket_helpers import API_KEY

    base, _ = websocket_runtime
    with (
        OpenAI(
            api_key=API_KEY, base_url=base + "/v1", max_retries=0, timeout=3
        ) as client,
        ExitStack() as connections,
    ):
        for _ in range(10):
            connections.enter_context(client.responses.connect(max_retries=0))
        with pytest.raises(InternalServerError) as error:
            client.models.list()
        assert error.value.status_code == 503
        assert error.value.response.headers["retry-after"] == "1"
        assert "concurrency limit" in error.value.message


async def test_contract_websocket_saturation_recovers_under_load(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    import asyncio
    from contextlib import AsyncExitStack

    from openai import InternalServerError

    from tests.websocket_helpers import API_KEY, MODEL, receive_terminal

    base, _ = websocket_runtime
    async with AsyncOpenAI(
        api_key=API_KEY, base_url=base + "/v1", max_retries=0, timeout=5
    ) as client:

        async def overloaded_request() -> None:
            with pytest.raises(InternalServerError) as error:
                await client.responses.create(model=MODEL, input="overloaded")
            assert error.value.status_code == 503
            assert error.value.response.headers["retry-after"] == "1"

        for cycle in range(10):
            async with AsyncExitStack() as connections:
                for _ in range(10):
                    await connections.enter_async_context(
                        client.responses.connect(max_retries=0)
                    )
                await asyncio.wait_for(
                    asyncio.gather(*(overloaded_request() for _ in range(50))), 10
                )
                async with httpx.AsyncClient() as health:
                    assert (await health.get(base + "/healthz")).status_code == 200
            # Exercise model traffic again after all capacity is released.
            async with client.responses.connect(max_retries=0) as connection:
                await connection.response.create(model=MODEL, input="recovery")
                response, _, _ = await receive_terminal(connection)
                assert response["status"] == "completed"
            print(
                f"saturation cycle {cycle + 1}/10: 50 rejected, recovery completed",
                flush=True,
            )


async def test_contract_websocket_cache_keys(
    websocket_runtime: tuple[str, list[dict[str, Any]]],
) -> None:
    from tests.websocket_helpers import API_KEY, MODEL, receive_terminal

    base, received = websocket_runtime
    async with (
        AsyncOpenAI(api_key=API_KEY, base_url=base + "/v1") as client,
        client.responses.connect(max_retries=0) as connection,
    ):
        for key in [None, None, "explicit-sdk-cache"]:
            kwargs: dict[str, Any] = {} if key is None else {"prompt_cache_key": key}
            await connection.response.create(
                model=MODEL, input="recovery", **kwargs
            )
            response, _, _ = await receive_terminal(connection)
            assert response["status"] == "completed"
    assert received[0]["prompt_cache_key"].startswith("auto_")
    assert received[0]["prompt_cache_key"] == received[1]["prompt_cache_key"]
    assert received[2]["prompt_cache_key"] == "explicit-sdk-cache"
