# The learning track (PLAN.md Phase 6)

The old RL died because the policy had to emit header-exact, docs-exact
symbols. The Phase 1 host enforces those mechanically (sentinel masking,
`apply_body_edit`, declaration checks), so the learnable skill is exactly
"shorter equivalent Rust". Everything here rides the same gate stack.

## Pipeline

```
build_manifest.py   mechanical cohort selection (crates.io ranking,
                    license + blocklist + LOC + fresh-clone test gates)
      |
anticontamination   the exam (bench/corpus.toml) never enters training data;
                    enforced by unit test, violation fails the build
      |
mine.py             production rung over <= 30 training crates; ACCEPTED
                    proposals -> pairs/sft.jsonl, rejects -> rejects.jsonl
      |
sft.py              QLoRA SFT (Qwen2.5-Coder-3B-Instruct, NF4, r=32/a=16,
                    8GB discipline). --validate-config runs on CPU.
      |
rft.py              rejection-sampling rounds (k=8, temp 1.0, top-p 0.95,
                    seed sweep) through the FULL cascade; keeps become the
                    union training set; early stop at < 2pp accept gain
      |
eval_proposer.py    held-out F3 eval (role = "eval" crates, equal budget);
                    promotion only if accepted-LOC/call >= base's
```

## Serving a fine-tuned policy through ollama (offline path)

Merge the adapter, convert, register:

```bash
uv run python training/sft.py --pairs training/pairs/sft.jsonl
# merge adapter onto the base model:
uv run python -c "
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer
m = AutoModelForCausalLM.from_pretrained('Qwen/Qwen2.5-Coder-3B-Instruct')
m = PeftModel.from_pretrained(m, 'training/adapter/sft/adapter')
m = m.merge_and_unload()
m.save_pretrained('training/adapter/merged')
AutoTokenizer.from_pretrained('Qwen/Qwen2.5-Coder-3B-Instruct').save_pretrained('training/adapter/merged')
"
# convert to GGUF (llama.cpp):
python3 llama.cpp/convert_hf_to_gguf.py training/adapter/merged \
    --outfile training/adapter/merged.gguf --outtype q8_0
# register and serve:
~/.local/ollama/bin/ollama create lc-sft -f training/Modelfile
# training/Modelfile:
#   FROM ./training/adapter/merged.gguf
#   TEMPLATE {{- if .System }}<|im_start|>system
#   {{ .System }}<|im_end|>
#   {{ end }}{{- range .Messages }}{{- if eq .Role "user" }}<|im_start|>user
#   {{ .Content }}<|im_end|>
#   {{ end }}{{- if eq .Role "assistant" }}<|im_start|>assistant
#   {{ .Content }}<|im_end|>
#   {{ end }}{{- end }}
```

After `ollama create`, run the mining/eval scripts with
`--rung-command "python3 bench/ollama_file.py lc-sft"`.

## Rules that do not bend

* The four benchmark crates never appear in any prompt, pair, or few-shot
  example. `training/anticontamination.py` scans everything; tests fail the
  build on a hit.
* role = "eval" crates are never fine-tuned on.
* Every "reward" in RFT is a full gate pass (tests + oracle), never a proxy.
* Promotion to the benchmark requires the held-out win at equal budget;
  otherwise the negative is recorded in training/EVAL.md.
