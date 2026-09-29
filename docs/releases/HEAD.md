# HEAD

- Change the Go backend timeout default to 1800 seconds of upstream inactivity.
  Long-running streams may exceed 1800 seconds while data continues arriving;
  a stalled stream is terminated only after the idle interval.

- Preserve encrypted reasoning items, their IDs and assistant phases when
  rebuilding `previous_response_id` context, including streamed responses.
- Preserve native assistant messages and function-call items during replay,
  including IDs, status, content blocks, annotations and original whitespace.

- Generate stable prompt cache routing keys by default, with CLI, environment,
  and TOML controls preserved through daemon startup. Explicit client keys take
  precedence, and per-request tracking IDs are independent of cache keys.
- Forward Chat Completions prompt cache keys and preserve cached-input and
  reasoning-token usage details in streaming and non-streaming responses.
- Reject upstream streams that end without a terminal response instead of
  reporting a successful partial or empty answer.
- Respect XDG configuration paths consistently for loading and generation, and
  default daemon files to the selected configuration directory's `run/` folder.
- Add deterministic SDK regressions and an opt-in live cache comparison. Prevent
  larger SDK test runs from blocking on unread subprocess log pipes.
