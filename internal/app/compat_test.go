package app

import (
	"encoding/json"
	"testing"
)

func TestChatUsageDoesNotInventMissingDetails(t *testing.T) {
	for _, cached := range []any{nil, 0, 128} {
		usage := map[string]any{"input_tokens": 256, "output_tokens": 20}
		if cached != nil {
			usage["input_tokens_details"] = map[string]any{"cached_tokens": cached}
			usage["output_tokens_details"] = map[string]any{"reasoning_tokens": 0}
		}
		chat := responseToChat(map[string]any{"usage": usage}, defaultModel, false, 1)
		converted := mapAny(chat["usage"])
		if cached == nil {
			if converted["prompt_tokens_details"] != nil || converted["completion_tokens_details"] != nil {
				t.Fatal("missing details were invented")
			}
		} else if mapAny(converted["prompt_tokens_details"])["cached_tokens"] != cached || mapAny(converted["completion_tokens_details"])["reasoning_tokens"] != 0 {
			t.Fatalf("usage = %v", converted)
		}
	}
}

func TestChatToResponseTranslatesMessagesToolsAndStructuredOutput(t *testing.T) {
	body := map[string]any{
		"model": "gpt-test",
		"messages": []any{
			map[string]any{"role": "system", "content": "Be terse."},
			map[string]any{"role": "user", "content": []any{map[string]any{"type": "text", "text": "weather"}}},
		},
		"tools":           []any{map[string]any{"type": "function", "function": map[string]any{"name": "lookup", "parameters": map[string]any{"type": "object"}}}},
		"tool_choice":     map[string]any{"type": "function", "function": map[string]any{"name": "lookup"}},
		"response_format": map[string]any{"type": "json_schema", "json_schema": map[string]any{"name": "answer", "strict": true, "schema": map[string]any{"type": "object"}}},
	}
	got := chatToResponse(body, defaultModel)
	if got["instructions"] != "Be terse." {
		t.Fatalf("instructions = %#v", got["instructions"])
	}
	input := sliceAny(got["input"])
	if len(input) != 1 || mapAny(input[0])["role"] != "user" {
		t.Fatalf("input = %#v", input)
	}
	tools := sliceAny(got["tools"])
	if len(tools) != 1 || mapAny(tools[0])["name"] != "lookup" {
		t.Fatalf("tools = %#v", tools)
	}
	choice := mapAny(got["tool_choice"])
	if choice["name"] != "lookup" {
		t.Fatalf("tool choice = %#v", choice)
	}
	format := mapAny(mapAny(got["text"])["format"])
	if format["type"] != "json_schema" || format["name"] != "answer" {
		t.Fatalf("format = %#v", format)
	}
}

func TestResponseAndChatStoresAreBoundedAndIsolated(t *testing.T) {
	responses := newResponseStore(1)
	responses.remember("one", []any{map[string]any{"role": "user", "content": "one"}}, map[string]any{"id": "one", "output": []any{outputMessage("answer")}})
	responses.remember("two", []any{}, map[string]any{"id": "two", "output": []any{}})
	if responses.get("one") != nil {
		t.Fatal("oldest response was not evicted")
	}
	got := responses.get("two")
	got.Response["status"] = "mutated"
	if responses.get("two").Response["status"] == "mutated" {
		t.Fatal("store leaked mutable state")
	}

	chats := newChatStore(1)
	completion := map[string]any{"id": "chatcmpl_one", "model": "gpt-test", "choices": []any{map[string]any{"index": 0, "message": map[string]any{"role": "assistant", "content": "hi"}}}}
	chats.remember("chatcmpl_one", completion, map[string]any{"key": "value"})
	stored := chats.get("chatcmpl_one")
	if len(stored.Messages) != 1 || stored.Messages[0]["id"] != "chatcmpl_one_msg_0" {
		t.Fatalf("messages = %#v", stored.Messages)
	}
	if _, err := json.Marshal(stored.Completion); err != nil {
		t.Fatal(err)
	}
}

func TestResponseStoreRefreshesExistingEntryEvictionOrder(t *testing.T) {
	responses := newResponseStore(2)
	responses.remember("one", []any{}, map[string]any{"id": "one", "output": []any{}})
	responses.remember("two", []any{}, map[string]any{"id": "two", "output": []any{}})
	responses.remember("one", []any{}, map[string]any{"id": "one", "output": []any{}})
	responses.remember("three", []any{}, map[string]any{"id": "three", "output": []any{}})
	if responses.get("one") == nil || responses.get("two") != nil || responses.get("three") == nil {
		t.Fatalf("unexpected eviction order: %#v", responses.order)
	}
}

func TestPrepareResponseNormalizesReasoningAndDefaults(t *testing.T) {
	got := prepareResponse(map[string]any{"input": []any{
		map[string]any{"type": "reasoning", "summary": []any{}},
		map[string]any{"type": "reasoning", "encrypted_content": "cipher", "summary": []any{}},
	}}, "gpt-default")
	if got["model"] != "gpt-default" || got["store"] != false {
		t.Fatalf("defaults = %#v", got)
	}
	input := sliceAny(got["input"])
	if len(input) != 1 || mapAny(input[0])["encrypted_content"] != "cipher" {
		t.Fatalf("input = %#v", input)
	}
}

func TestResponseContextPreservesEncryptedReasoningAndPhase(t *testing.T) {
	output := []any{
		map[string]any{"type": "reasoning", "id": "rs_original", "encrypted_content": "opaque-cipher", "summary": []any{map[string]any{"type": "summary_text", "text": "summary"}}},
		map[string]any{"type": "message", "role": "assistant", "phase": "commentary", "content": []any{map[string]any{"type": "output_text", "text": "working"}}},
		map[string]any{"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
	}
	store := newResponseStore(2)
	store.remember("resp_1", []any{map[string]any{"role": "user", "content": "hello"}}, map[string]any{"output": output})
	context := store.get("resp_1").Context
	if len(context) != 4 || mapAny(context[1])["encrypted_content"] != "opaque-cipher" || mapAny(context[1])["id"] != "rs_original" || mapAny(context[2])["phase"] != "commentary" || mapAny(context[3])["call_id"] != "call_1" {
		t.Fatalf("reasoning, order, or phase was lost")
	}
	mapAny(context[1])["encrypted_content"] = "changed"
	if mapAny(store.get("resp_1").Context[1])["encrypted_content"] != "opaque-cipher" {
		t.Fatal("stored reasoning was mutated")
	}
}

func TestReplayPreservesEveryNativeOutputField(t *testing.T) {
	native := []any{
		map[string]any{"type": "reasoning", "id": "rs_1", "status": "completed", "encrypted_content": "cipher", "content": []any{}, "summary": []any{map[string]any{"type": "summary_text", "text": "summary"}}},
		map[string]any{"type": "message", "id": "msg_1", "status": "completed", "role": "assistant", "phase": "final_answer", "content": []any{map[string]any{"type": "output_text", "text": "\n\nanswer", "annotations": []any{}}}},
		map[string]any{"type": "function_call", "id": "fc_1", "status": "completed", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
	}
	for _, replay := range []any{normalizeResponseInput(native), responseContext(map[string]any{"output": native})} {
		want, _ := json.Marshal(native)
		got, _ := json.Marshal(replay)
		if string(want) != string(got) {
			t.Fatal("native output fields were changed during replay")
		}
	}
}
