"""Local Ollama adapter for the whole-file rewrite contract (lc rewrite)."""

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
        "think": False,
        "stream": False,
        "options": {
            # retries sample warmer, or a temp-0 policy just repeats the
            # rejected output and the funnel fills with duplicates
            "temperature": 0.4 if prompt.get("feedback") is not None else 0,
            "seed": 42,
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
