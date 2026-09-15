"""OpenAI-compatible remote-model adapter; the ladder's strong rung.

Reads LC_ML_API_BASE, LC_ML_API_KEY, LC_ML_MODEL from the environment.
Same JSON-in/JSON-out contract as bench/ollama.py: one prompt payload on
stdin, the raw model text on stdout. The key is read from the env at call
time, never logged, never stored in records; the recorded digest is the
model id plus the response `system_fingerprint`.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main():
    prompt = json.load(sys.stdin)
    base = os.environ["LC_ML_API_BASE"].rstrip("/")
    key = os.environ["LC_ML_API_KEY"]
    model = os.environ["LC_ML_MODEL"]
    system = prompt.pop("system")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(prompt)},
        ],
        "temperature": 0,
        "max_tokens": 8192,
    }
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print(
            f"remote model error: HTTP {exc.code}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    sys.stdout.write(result["choices"][0]["message"]["content"])
    fingerprint = result.get("system_fingerprint", "none")
    print(f'{{"digest": "{model}/{fingerprint}"}}', file=sys.stderr)


if __name__ == "__main__":
    main()
