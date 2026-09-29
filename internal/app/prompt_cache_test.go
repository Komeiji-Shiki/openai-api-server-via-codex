package app

import (
	"bytes"
	"log"
	"net/http"
	"strings"
	"testing"
)

func TestPromptCacheTemplateGrouping(t *testing.T) {
	payload := prepareResponse(map[string]any{
		"instructions": "Private stable instructions",
		"input":        "first question",
		"tools":        []any{map[string]any{"type": "function", "name": "lookup"}},
	}, defaultModel)
	key := defaultPromptCacheKey(payload)
	if !strings.HasPrefix(key, "auto_") || len(key) > 64 || strings.Contains(key, "Private") {
		t.Fatalf("key = %q", key)
	}
	continued := cloneMap(payload)
	continued["input"] = append(sliceAny(continued["input"]), map[string]any{"role": "assistant", "content": "answer"}, map[string]any{"role": "user", "content": "followup"})
	if defaultPromptCacheKey(continued) != key {
		t.Fatal("appending a conversation changed the template key")
	}
	for _, field := range []string{"model", "instructions", "tools", "user"} {
		changed := cloneMap(payload)
		changed[field] = "another template"
		if defaultPromptCacheKey(changed) == key {
			t.Fatalf("changing %s did not separate templates", field)
		}
	}
	developer := cloneMap(payload)
	developer["input"] = []any{map[string]any{"role": "developer", "content": "Private prefix"}, map[string]any{"role": "user", "content": "first"}}
	if defaultPromptCacheKey(developer) == key {
		t.Fatal("leading developer message was ignored")
	}
}

func TestPromptCacheKeyPolicyAndRequestIDs(t *testing.T) {
	for _, tc := range []struct {
		name                 string
		auto, explicit, drop bool
		value                any
	}{
		{name: "automatic", auto: true},
		{name: "disabled"},
		{name: "explicit", auto: true, explicit: true, value: "session-123"},
		{name: "explicit while disabled", explicit: true, value: "session-123"},
		{name: "explicit empty", auto: true, explicit: true, value: ""},
		{name: "explicit null", auto: true, explicit: true},
		{name: "drop automatic", auto: true, drop: true},
		{name: "drop explicit", auto: true, explicit: true, value: "session-123", drop: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			environment := newContractEnvironment(t, func(cfg *config) {
				cfg.AutoPromptCacheKey = tc.auto
				if tc.drop {
					cfg.DropParams = []string{"prompt_cache_key"}
				}
			})
			payload := map[string]any{"input": "cache policy"}
			if tc.explicit {
				payload["prompt_cache_key"] = tc.value
			}
			var previousID string
			for i := 0; i < 2; i++ {
				environment.json(t, http.MethodPost, "/v1/responses", payload, http.StatusOK)
				req := environment.upstream.LastResponseRequest(t)
				key, present := req.JSON["prompt_cache_key"]
				switch {
				case tc.drop || (!tc.auto && !tc.explicit):
					if present {
						t.Fatalf("unexpected key: %v", key)
					}
				case tc.explicit:
					if !present || key != tc.value {
						t.Fatalf("explicit key = %v", key)
					}
				default:
					if !strings.HasPrefix(stringValue(key), "auto_") {
						t.Fatalf("automatic key = %v", key)
					}
				}
				if req.Headers.Get("session_id") != stringValue(key) {
					t.Fatal("session header differs from key")
				}
				id := req.Headers.Get("x-client-request-id")
				if id == "" || id == previousID || id == stringValue(key) {
					t.Fatalf("request ID = %q", id)
				}
				previousID = id
			}
		})
	}
}

func TestPromptCacheDoesNotLogPrivateValues(t *testing.T) {
	var logs bytes.Buffer
	previous := log.Writer()
	log.SetOutput(&logs)
	defer log.SetOutput(previous)
	environment := newContractEnvironment(t, func(cfg *config) { cfg.Verbose = true })
	environment.json(t, http.MethodPost, "/v1/chat/completions", map[string]any{
		"prompt_cache_key": "private-session-secret",
		"messages":         []any{map[string]any{"role": "system", "content": "private-template-secret"}, map[string]any{"role": "user", "content": "hello"}},
	}, http.StatusOK)
	for _, secret := range []string{"private-session-secret", "private-template-secret"} {
		if strings.Contains(logs.String(), secret) {
			t.Fatalf("private value leaked in logs: %s", secret)
		}
	}
}
