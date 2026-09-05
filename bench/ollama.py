"""Local Ollama adapter for lc --ml-cli; no credentials or hosted API required."""

import json
import sys
import urllib.request


def main():
    prompt = json.load(sys.stdin)
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5-coder:7b"
    reasoning = model.startswith("qwen3")
    payload = {
        "model": model,
        "system": prompt.pop("system"),
        "prompt": json.dumps(prompt),
        "format": {
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "replacement": {"type": "string"},
            },
            "required": ["symbol_id", "replacement"],
            "additionalProperties": False,
        },
        "stream": False,
        "options": {
            "temperature": 0,
            "seed": 42,
            "num_ctx": 16384,
            "num_predict": 4096 if reasoning else 2048,
        },
    }
    if reasoning:
        payload["think"] = True
    payload["system"] += (
        " If no safe reduction exists, return the unchanged symbol as replacement."
    )
    request = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=270 if reasoning else 90) as response:
        result = json.load(response)
    print(result["response"])


if __name__ == "__main__":
    main()
