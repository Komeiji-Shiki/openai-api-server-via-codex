from typing import Any


def flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(filter(None, (flatten_text(item) for item in value)))
    if isinstance(value, dict):
        if value.get("type") in {"input_text", "output_text", "text"}:
            return str(value.get("text") or "")
        return " ".join(
            filter(
                None, (flatten_text(value.get(key)) for key in ("content", "input"))
            )
        )
    return ""
