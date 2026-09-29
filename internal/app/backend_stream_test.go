package app

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestBackendRequiresTerminalResponse(t *testing.T) {
	for _, tc := range []struct {
		name, data string
		wantError  bool
	}{
		{"empty", "", true},
		{"done only", "data: [DONE]\n\n", true},
		{"delta only", "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n", true},
		{"malformed", "data: {invalid}\n\n", true},
		{"missing response", "data: {\"type\":\"response.completed\"}\n\n", true},
		{"completed", "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\n\n", false},
		{"final event at EOF", "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}", false},
		{"incomplete", "data: {\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\"}}\n\n", false},
		{"failed", "data: {\"type\":\"response.failed\",\"response\":{\"status\":\"failed\"}}\n\n", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, tc.data) }))
			defer upstream.Close()
			b := retryTestBackend(t, upstream, writeTestAuth(t, authDocument("test-token", nil)))
			response, err := b.collect(context.Background(), map[string]any{"model": defaultModel})
			if tc.wantError {
				var backendErr *backendError
				if !errors.As(err, &backendErr) || backendErr.Status != 502 || response != nil {
					t.Fatalf("response=%v err=%v", response, err)
				}
			} else if err != nil || response == nil {
				t.Fatalf("response=%v err=%v", response, err)
			}
		})
	}
}

func TestBackendStreamTimeoutIsIdleAndAllowsLongActiveStreams(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		for _, event := range []string{
			`data: {"type":"response.created","response":{"status":"in_progress"}}` + "\n\n",
			`data: {"type":"response.completed","response":{"status":"completed"}}` + "\n\n",
		} {
			_, _ = io.WriteString(w, event)
			flusher.Flush()
			time.Sleep(35 * time.Millisecond)
		}
	}))
	defer upstream.Close()
	authPath := writeTestAuth(t, authDocument("test-token", nil))
	cfg := defaultConfig()
	cfg.BackendURL = upstream.URL
	cfg.AuthJSON = authPath
	cfg.Timeout = 50 * time.Millisecond
	b := newBackend(cfg)
	if _, err := b.collect(context.Background(), map[string]any{"model": defaultModel}); err != nil {
		t.Fatalf("active stream exceeded total timeout unexpectedly: %v", err)
	}
}

func TestBackendStreamIdleTimeoutReturnsGatewayError(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		flusher := w.(http.Flusher)
		_, _ = io.WriteString(w, `data: {"type":"response.created","response":{"status":"in_progress"}}`+"\n\n")
		flusher.Flush()
		time.Sleep(80 * time.Millisecond)
	}))
	defer upstream.Close()
	authPath := writeTestAuth(t, authDocument("test-token", nil))
	cfg := defaultConfig()
	cfg.BackendURL = upstream.URL
	cfg.AuthJSON = authPath
	cfg.Timeout = 25 * time.Millisecond
	b := newBackend(cfg)
	_, err := b.collect(context.Background(), map[string]any{"model": defaultModel})
	var backendErr *backendError
	if !errors.As(err, &backendErr) || backendErr.Status != 502 || !strings.Contains(err.Error(), "idle timeout") {
		t.Fatalf("err = %v", err)
	}
}
