"""SFT warm start on mined (system, prompt, response) pairs (roadmap F1).

Why SFT before GRPO: the measured failure distribution of the base 7B on
mature external repos is ~40% doc-eating and ~30% whole-file confusion —
compliance failures, not capability failures. Every mined pair passed the
full gate (tests + API + docs + smaller), so supervised training on them
teaches output shape and doc preservation directly; GRPO then only has to
improve the semantic part, from a policy whose rollouts are not mostly -1
(sparse-reward groups carry no gradient).

Data: `lc reduce --mine-out pairs.jsonl` rows {system, prompt, response}.
Trained as conversations (messages) — the same format train.py uses, since
*-Instruct models degenerate on raw-string prompts (instant-EOS).

Usage:
    uv run python grpo/sft.py --pairs grpo/pairs.jsonl --steps 200
    uv run python grpo/sft.py --validate-config --pairs grpo/pairs.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "model": "Qwen/Qwen2.5-Coder-3B-Instruct",
    "4bit": True,          # RTX 3070 8GB: QLoRA or nothing
    "lora_r": 32,
    "lora_alpha": 16,
    "lr": 2e-5,
    "epochs": 2,
    "per_device_batch": 2,
    "grad_accum": 4,
    "max_prompt_tokens": 2048,  # same cap as train.py; pairs are already gated
    "max_length": 3072,         # prompt cap + completion headroom
    "min_pairs": 50,
}


def load_pairs(path: Path) -> list[dict]:
    rows = [json.loads(l) for l in path.open() if l.strip()]
    conversations = []
    for r in rows:
        conversations.append({
            "messages": [
                {"role": "system", "content": r["system"]},
                {"role": "user", "content": r["prompt"]},
                {"role": "assistant", "content": r["response"]},
            ]
        })
    return conversations


def validate_config(cfg: dict, pairs: Path) -> list[str]:
    errors: list[str] = []
    if not pairs.exists():
        errors.append(f"pairs file not found: {pairs}")
        return errors
    rows = [json.loads(l) for l in pairs.open() if l.strip()]
    if len(rows) < cfg["min_pairs"]:
        errors.append(
            f"only {len(rows)} pairs (min {cfg['min_pairs']}): mine more with "
            "`lc reduce --mine-out` over varied targets/temperatures — SFT on "
            "dozens of pairs memorizes instead of generalizing"
        )
    for i, r in enumerate(rows):
        for field in ("system", "prompt", "response"):
            if not r.get(field):
                errors.append(f"row {i} missing field {field}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", type=Path, default=REPO / "grpo" / "pairs.jsonl")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--steps", type=int, default=200)
    ap.add_argument("--epochs", type=int, default=DEFAULTS["epochs"])
    ap.add_argument("--out", type=Path, default=REPO / "grpo" / "sft-adapter")
    ap.add_argument("--validate-config", action="store_true")
    args = ap.parse_args()

    cfg = {**DEFAULTS, "model": args.model}
    if args.validate_config:
        errors = validate_config(cfg, args.pairs)
        if errors:
            print("config errors:")
            for e in errors:
                print(f"  - {e}")
            return 1
        print(f"config ok: {len(load_pairs(args.pairs))} pairs")
        return 0

    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
        )
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        print(f"training deps missing ({exc}); see grpo/train.py header")
        return 1

    conversations = load_pairs(args.pairs)
    print(f"{len(conversations)} pairs | model {cfg['model']}")

    tok = AutoTokenizer.from_pretrained(cfg["model"])

    def _cap(messages: list[dict]) -> list[dict]:
        ids = tok(messages[1]["content"]).input_ids
        if len(ids) <= cfg["max_prompt_tokens"]:
            return messages
        keep_head = 512
        keep_tail = cfg["max_prompt_tokens"] - keep_head
        messages[1]["content"] = tok.decode(ids[:keep_head] + ids[-keep_tail:])
        return messages

    ds = Dataset.from_list([
        {"messages": _cap([*c["messages"]])} for c in conversations
    ])

    quant = (
        BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
        )
        if cfg["4bit"] else None
    )
    model = AutoModelForCausalLM.from_pretrained(
        cfg["model"], quantization_config=quant, torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    lora = LoraConfig(
        r=cfg["lora_r"], lora_alpha=cfg["lora_alpha"], lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    sft_cfg = SFTConfig(
        output_dir=str(args.out),
        max_steps=args.steps,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=cfg["per_device_batch"],
        gradient_accumulation_steps=cfg["grad_accum"],
        learning_rate=cfg["lr"],
        bf16=True,
        logging_steps=5,
        save_strategy="steps",   # the GPU is shared: a kill at step 37 loses
        save_steps=50,           # everything otherwise
        save_total_limit=2,
        report_to=[],
        max_length=cfg["max_length"],
    )
    trainer = SFTTrainer(model=model, args=sft_cfg, train_dataset=ds,
                         peft_config=lora, processing_class=tok)
    trainer.train()
    trainer.save_model(str(args.out))
    print(f"adapter saved: {args.out}")
    print("next: GRPO from this adapter (grpo/train.py --model <adapter>) "
          "or eval as a proposer in the same pipeline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
