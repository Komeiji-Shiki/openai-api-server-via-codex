package app

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
)

func TestWebSocketPromptCachePolicyAndRequestIdentity(t *testing.T) {
	for _, tc := range []struct {
		name, explicitKey, sessionID, requestID string
		auto, drop                              bool
	}{
		{name: "automatic", auto: true, requestID: "client-trace"},
		{name: "disabled"},
		{name: "explicit while disabled", explicitKey: "client-cache", sessionID: "client-session", requestID: "client-trace"},
		{name: "dropped", auto: true, drop: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			captured := make(chan http.Header, 1)
			_, target := websocketTestServer(t, func(w http.ResponseWriter, r *http.Request) {
				captured <- r.Header.Clone()
				conn, err := websocket.Accept(w, r, nil)
				if err != nil {
					return
				}
				defer conn.CloseNow()
				for {
					var event map[string]any
					if wsjson.Read(r.Context(), conn, &event) != nil || wsjson.Write(r.Context(), conn, event) != nil {
						return
					}
				}
			}, func(cfg *config) {
				cfg.AutoPromptCacheKey = tc.auto
				if tc.drop {
					cfg.DropParams = []string{"prompt_cache_key"}
				}
			})
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			headers := make(http.Header)
			headers.Set("Authorization", "Bearer "+contractAPIKey)
			headers.Set("session_id", tc.sessionID)
			headers.Set("x-client-request-id", tc.requestID)
			conn, _, err := websocket.Dial(ctx, target, &websocket.DialOptions{HTTPHeader: headers})
			if err != nil {
				t.Fatal(err)
			}
			defer conn.CloseNow()
			upstreamHeaders := <-captured
			if upstreamHeaders.Get("session_id") != tc.sessionID {
				t.Fatal("request tracing was incorrectly used as the connection session")
			}
			requestID := upstreamHeaders.Get("x-client-request-id")
			if requestID == "" || (tc.requestID != "" && requestID != tc.requestID) {
				t.Fatalf("request ID was lost: %q", requestID)
			}
			var previousKey string
			for turn := 0; turn < 2; turn++ {
				event := map[string]any{"type": "response.create", "model": "gpt-6-astra", "instructions": "stable template", "input": fmt.Sprintf("turn %d", turn)}
				if tc.explicitKey != "" {
					event["prompt_cache_key"] = tc.explicitKey
				}
				if err := wsjson.Write(ctx, conn, event); err != nil {
					t.Fatal(err)
				}
				var received map[string]any
				if err := wsjson.Read(ctx, conn, &received); err != nil {
					t.Fatal(err)
				}
				key, present := received["prompt_cache_key"]
				switch {
				case tc.drop || (!tc.auto && tc.explicitKey == ""):
					if present {
						t.Fatalf("unexpected cache key: %v", key)
					}
				case tc.explicitKey != "":
					if key != tc.explicitKey {
						t.Fatal("explicit cache key was changed")
					}
				default:
					if !strings.HasPrefix(stringValue(key), "auto_") || (turn > 0 && key != previousKey) {
						t.Fatal("automatic cache key was not stable across turns")
					}
				}
				if present && key == requestID {
					t.Fatal("cache key and request tracing must remain separate")
				}
				previousKey = stringValue(key)
			}
		})
	}
}
