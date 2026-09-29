package app

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// Group requests by their stable template, never by the growing conversation.
// Explicit keys are the preferred way for clients to select a session or shard.
func defaultPromptCacheKey(payload map[string]any) string {
	template := map[string]any{}
	for _, key := range []string{"model", "instructions", "tools", "user"} {
		if payload[key] != nil {
			template[key] = payload[key]
		}
	}
	var prefix []any
	for _, raw := range sliceAny(payload["input"]) {
		item := mapAny(raw)
		if item["role"] != "system" && item["role"] != "developer" {
			break
		}
		prefix = append(prefix, item)
	}
	if len(prefix) > 0 {
		template["prefix"] = prefix
	}
	data, _ := json.Marshal(template)
	digest := sha256.Sum256(data)
	return "auto_" + hex.EncodeToString(digest[:24])
}
