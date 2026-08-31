"""GRPO training for LOC reduction — Qwen2.5-Coder QLoRA sized for 8GB VRAM.

Real training (GPU):
  python grpo/train.py --steps 10 --model Qwen/Qwen2.5-Coder-0.5B-Instruct

Config validation (CPU only, no model load):
  python grpo/train.py --validate-config
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULTS = {
    "model": "Qwen/Qwen2.5-Coder-0.5B-Instruct",
    "lora_r": 32,
    "lora_alpha": 16,
    "num_generations": 4,          # group size G; divides generation batch
    "per_device_batch": 2,         # 2 prompts x 4 gens = 8 completions/step
    "max_completion_length": 384,  # units are small: keep completions short
    "max_prompt_length": 4096,
    "lr": 2e-5,
    "beta": 0.0,                   # KL off (Dr.GRPO/DAPO practice)
    "loss_type": "dr_grpo",        # constant norm: no length bias (task IS shorter output)
    "scale_rewards": "batch",
    "bf16": True,
    "gradient_checkpointing": True,
    "4bit": True,                  # QLoRA NF4 + double quant
}


def validate_config(cfg: dict, dataset: Path) -> list[str]:
    errors = []
    if not dataset.is_file():
        errors.append(f"dataset missing: {dataset} (run grpo/build_dataset.py first)")
    else:
        rows = [json.loads(l) for l in dataset.open()][:5]
        if not rows:
            errors.append("dataset is empty")
        for i, row in enumerate(rows):
            for field in ("prompt", "fixture", "lang", "unit_source", "unit_loc"):
                if field not in row:
                    errors.append(f"dataset row {i} missing field {field}")
    if cfg["num_generations"] < 2:
        errors.append("num_generations must be >= 2 (group-relative advantage needs a group)")
    if cfg["per_device_batch"] * cfg["num_generations"] > 16:
        errors.append("generation batch too large for 8GB VRAM")
    if cfg["max_completion_length"] > 1024:
        errors.append("max_completion_length > 1024 will OOM the rollout cache on 8GB")
    if "/" not in cfg["model"]:
        errors.append("model must be a HF hub id (org/name)")
    try:
        import rewards as grpo_rewards

        # C6 names both variants; a missing one is a config error, not a surprise
        # at step 1 of a GPU run.
        for name in ("reward_fn", "reward_fn_mutation_weighted"):
            if not callable(getattr(grpo_rewards, name, None)):
                errors.append(f"reward module has no callable {name}")
    except ImportError as exc:
        errors.append(f"reward module not importable: {exc}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--dataset", type=Path, default=REPO / "grpo" / "dataset.jsonl")
    ap.add_argument(
        "--reward", default="loc", choices=["loc", "mutation-weighted"],
        help="loc = gate x (1 + lambda*loc_delta) - penalties; mutation-weighted "
             "additionally scales the shaping term by the sample's suite kill rate "
             "(needs `mutation_score` in the dataset; absent means 1.0)",
    )
    ap.add_argument("--steps", type=int, default=10)
    ap.add_argument("--validate-config", action="store_true")
    args = ap.parse_args()

    cfg = {**DEFAULTS, "model": args.model}
    if args.validate_config:
        errors = validate_config(cfg, args.dataset)
        if errors:
            for e in errors:
                print(f"CONFIG ERROR: {e}", file=sys.stderr)
            return 1
        print(json.dumps(cfg, indent=2))
        print("CONFIG OK")
        return 0

    # ---- real training path (GPU) ----
    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
        from trl import GRPOConfig, GRPOTrainer
    except ImportError as exc:
        print(f"training deps missing ({exc}); install with: "
              f"uv pip install trl peft transformers datasets torch", file=sys.stderr)
        return 2

    from rewards import reward_fn, reward_fn_mutation_weighted

    reward = reward_fn_mutation_weighted if args.reward == "mutation-weighted" else reward_fn
    print(f"reward: {reward.__name__}")

    rows = [json.loads(l) for l in args.dataset.open()]
    ds = Dataset.from_list(
        [{"prompt": r["prompt"], "sample_meta": r} for r in rows]
    )
    quant = (
        BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                           bnb_4bit_use_double_quant=True)
        if cfg["4bit"] else None
    )
    tok = AutoTokenizer.from_pretrained(cfg["model"])
    model = AutoModelForCausalLM.from_pretrained(
        cfg["model"], quantization_config=quant, torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    lora = LoraConfig(
        r=cfg["lora_r"], lora_alpha=cfg["lora_alpha"], lora_dropout=0.05,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    gcfg = GRPOConfig(
        output_dir=str(REPO / "grpo" / "out"),
        max_steps=args.steps,
        per_device_train_batch_size=cfg["per_device_batch"],
        num_generations=cfg["num_generations"],
        max_completion_length=cfg["max_completion_length"],
        max_prompt_length=cfg["max_prompt_length"],
        learning_rate=cfg["lr"],
        beta=cfg["beta"],
        loss_type=cfg["loss_type"],
        scale_rewards=cfg["scale_rewards"],
        bf16=cfg["bf16"],
        gradient_checkpointing=cfg["gradient_checkpointing"],
        logging_steps=1,
        save_strategy="no",
        report_to=[],
    )
    trainer = GRPOTrainer(
        model=model,
        args=gcfg,
        train_dataset=ds,
        reward_funcs=[reward],
        peft_config=lora,
    )
    trainer.train()
    trainer.save_model(str(REPO / "grpo" / "adapter"))
    print("training complete; adapter in grpo/adapter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
