"""Ollama adapter for RFT sampling: temperature 1.0, top-p 0.95, seed sweep.

Same JSON-in/JSON-out contract as bench/ollama_file.py; the seed comes from
the payload's `seed` field (the caller sweeps it per sample).
"""

import json
import sys
import urllib.request


def main():
    prompt = json.load(sys.stdin)
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5-coder:7b"
    payload = {
        "model": model,
        "system": prompt["system"],
        "prompt": json.dumps(
            {
                "path": prompt["path"],
                "source": prompt["source"],
                "context": prompt["context"],
                "feedback": prompt["feedback"],
            }
        ),
        "format": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "stream": False,
        "options": {
            "temperature": 1.0,
            "top_p": 0.95,
            "seed": prompt.get("seed", 42),
            "num_ctx": 16384,
            "num_predict": 8192,
        },
    }
    request = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        result = json.load(response)
    print(result["response"])


if __name__ == "__main__":
    main()
