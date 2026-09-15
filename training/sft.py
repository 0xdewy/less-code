"""SFT warm start on gate-verified pairs (PLAN.md §6.3; the resurrected
grpo/sft.py discipline, rebuilt on the Phase 1 interfaces).

Qwen2.5-Coder-3B-Instruct, QLoRA NF4 (r=32, alpha=16), 8GB discipline:
per-device batch 2 x grad-accum 8, ctx 4096, completion cap 2048, checkpoint
every 50 steps, lr 1e-4, 2-3 epochs over <= ~2k pairs.

GPU (real training):
  uv run python training/sft.py --pairs training/pairs/sft.jsonl
CPU (config validation only, never loads a model):
  uv run python training/sft.py --validate-config --pairs training/pairs/sft.jsonl

After training, merge to GGUF and serve through ollama (exact commands in
training/README.md), then `ollama create lc-sft -f Modelfile`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DEFAULTS = {
    "model": "Qwen/Qwen2.5-Coder-3B-Instruct",
    "lora_r": 32,
    "lora_alpha": 16,
    "lr": 1e-4,
    "epochs": 2,
    "per_device_batch": 2,
    "grad_accum": 8,
    "max_length": 4096,
    "completion_cap": 2048,
    "save_steps": 50,
    "min_pairs": 50,
    "max_pairs": 2000,
}


def load_pairs(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.open() if line.strip()]
    if len(rows) > DEFAULTS["max_pairs"]:
        rows = rows[: DEFAULTS["max_pairs"]]
    return rows


def validate_config(pairs: Path) -> list[str]:
    errors: list[str] = []
    if not pairs.is_file():
        errors.append(f"pairs file not found: {pairs}")
    else:
        count = sum(1 for line in pairs.open() if line.strip())
        if count < DEFAULTS["min_pairs"]:
            errors.append(f"only {count} pairs (< {DEFAULTS['min_pairs']})")
    if DEFAULTS["per_device_batch"] * DEFAULTS["grad_accum"] > 16:
        errors.append("effective batch too large for 8GB VRAM")
    if DEFAULTS["completion_cap"] > 2048:
        errors.append("completion cap > 2048 will OOM the rollout cache on 8GB")
    try:
        import peft  # noqa: F401
        import transformers  # noqa: F401
        import trl  # noqa: F401

        try:
            import bitsandbytes  # noqa: F401
        except ImportError:
            errors.append("bitsandbytes missing: QLoRA NF4 requires it on GPU")
    except ImportError as exc:
        errors.append(
            f"learning-track deps not installed ({exc}); CPU --validate-config"
            " only checks what is checkable - install trl peft transformers"
            " bitsandbytes to train"
        )
    return errors


def train(pairs: Path, out_dir: Path, epochs: int) -> Path:
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    rows = load_pairs(pairs)
    dataset = Dataset.from_list([{"messages": row["messages"]} for row in rows])
    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        DEFAULTS["model"],
        quantization_config=quant,
        device_map="auto",
        torch_dtype=torch.bfloat16,
    )
    tokenizer = AutoTokenizer.from_pretrained(DEFAULTS["model"])
    config = SFTConfig(
        output_dir=str(out_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=DEFAULTS["per_device_batch"],
        gradient_accumulation_steps=DEFAULTS["grad_accum"],
        learning_rate=DEFAULTS["lr"],
        max_length=DEFAULTS["max_length"],
        save_steps=DEFAULTS["save_steps"],
        gradient_checkpointing=True,
        bf16=True,
        logging_steps=5,
        save_total_limit=2,
        report_to=[],
        completion_only_loss=True,
        max_completion_length=DEFAULTS["completion_cap"],
    )
    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=dataset,
        processing_class=tokenizer,
        peft_config=LoraConfig(
            r=DEFAULTS["lora_r"],
            lora_alpha=DEFAULTS["lora_alpha"],
            lora_dropout=0.05,
            task_type="CAUSAL_LM",
        ),
    )
    trainer.train()
    trainer.save_model(str(out_dir / "adapter"))
    return out_dir / "adapter"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", default="training/pairs/sft.jsonl")
    parser.add_argument("--out", default="training/adapter/sft")
    parser.add_argument("--epochs", type=int, default=DEFAULTS["epochs"])
    parser.add_argument("--validate-config", action="store_true")
    args = parser.parse_args()
    pairs = Path(args.pairs)
    errors = validate_config(pairs)
    if args.validate_config:
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("sft config ok (CPU path; no model loaded)")
        return 0
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    adapter = train(pairs, Path(args.out), args.epochs)
    print(f"adapter: {adapter}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
