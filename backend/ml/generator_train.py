"""Model B: CodeT5-small, fine-tuned to write SQL (Backend Plan §23, weeks 4-5).

Two stages, and the order is the point.

    stage 1   Spider, 7,000 questions over 140 databases. Teaches the model
              what SQL is, over schemas it will never see again.
    stage 2   this warehouse's verified synthetic pairs. Teaches it the six
              tables it will actually be asked about.

Stage 2 alone would give a model that writes fluent SQL for one schema and
nothing else. Stage 1 alone would give the published Spider numbers, which are
not what a business asks. The checkpoint that ships is stage 1 then stage 2.

CPU only, deliberately: the demo machine has no GPU, and a model that needs
one is not a model this project can show. 60M parameters is what makes that
tolerable -- and is why the fallback to Gemma exists for the questions it
cannot handle.

    python -m ml.generator_train --stage 1 --epochs 2
    python -m ml.generator_train --stage 2 --epochs 6

Both write a checkpoint the API can load; neither touches the running API.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time

from ml.paths import (  # noqa: E402 - sets USE_TF before transformers loads
    CHECKPOINTS, GENERATOR, GENERATOR_STAGE1, RESULTS, SPIDER, SYNTH, read_jsonl,
)
from ml.serialize import columns_for, to_input, training_context

BASE_MODEL = "Salesforce/codet5-small"
MAX_INPUT = 384
MAX_TARGET = 160
SEED = 20260923


def _rows_for_stage(stage: int, limit: int | None) -> tuple[list[dict], list[dict]]:
    if stage == 1:
        train = list(read_jsonl(SPIDER / "train.jsonl"))
        dev = list(read_jsonl(SPIDER / "dev.jsonl"))
    else:
        train = list(read_jsonl(SYNTH / "train.jsonl"))
        dev = list(read_jsonl(SYNTH / "dev.jsonl"))
    if limit:
        train, dev = train[:limit], dev[: max(8, limit // 10)]
    return train, dev


def _warehouse_columns():
    """The six tables and five foreign keys of the seeded warehouse.

    Taken from the registry rather than typed out again: what the model is
    trained on is what the API will send it.
    """
    from ml.warehouse import warehouse_schema  # noqa: PLC0415
    tables, foreign_keys = warehouse_schema()
    return columns_for(tables, foreign_keys)


def balance(rows: list[dict], target: int, rng: random.Random) -> list[dict]:
    """Give every question SHAPE the same weight, whatever its slots allow.

    The generator counts combinations, so a template with a customer slot and
    a period slot produces 178 pairs while "total sales by region" -- which
    has no slots and is the demo's first question -- produces three. Trained
    on that, the model learns the shapes it saw two hundred times and fumbles
    the ones it saw three, which is exactly backwards.

    Repeats are not duplicates: the schema handed to the model is built per
    example with a different set of distractor tables in a different order,
    so a repeated pair is a genuinely different input with the same answer.
    """
    if not target:
        return rows

    by_template: dict[str, list[dict]] = {}
    for row in rows:
        by_template.setdefault(row.get("template", "?"), []).append(row)

    out: list[dict] = []
    for group in by_template.values():
        if len(group) >= target:
            out.extend(rng.sample(group, target))
        else:
            out.extend(group)
            out.extend(rng.choice(group) for _ in range(target - len(group)))
    rng.shuffle(out)
    return out


def build_examples(rows: list[dict], stage: int, rng: random.Random) -> list[tuple[str, str]]:
    examples: list[tuple[str, str]] = []
    warehouse = _warehouse_columns() if stage == 2 else None

    for row in rows:
        if stage == 1:
            columns = columns_for(row["schema"]["tables"],
                                  row["schema"]["foreign_keys"])
        else:
            columns = warehouse
        schema = training_context(columns, row["tables"], rng)
        examples.append((to_input(row["question"], schema), row["sql"]))
    return examples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=int, choices=(1, 2), required=True)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--accumulate", type=int, default=2)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--limit", type=int, default=0,
                        help="use only N training rows -- for a smoke run")
    parser.add_argument("--balance", type=int, default=0,
                        help="pairs per question shape (stage 2): evens out "
                             "templates whose slots produce hundreds against "
                             "templates that produce three")
    parser.add_argument("--save-every", type=int, default=40,
                        help="write the checkpoint every N optimiser steps")
    parser.add_argument("--resume", action="store_true",
                        help="carry on from this stage's own checkpoint if it exists")
    # Physical cores, not the sixteen logical processors: hyperthreads share
    # the same vector units, which is what this is bound by.
    parser.add_argument("--threads", type=int, default=10)
    args = parser.parse_args()

    import torch  # noqa: PLC0415
    from torch.utils.data import DataLoader  # noqa: PLC0415
    from transformers import (  # noqa: PLC0415
        AutoTokenizer, T5ForConditionalGeneration, get_linear_schedule_with_warmup,
    )

    if args.threads:
        torch.set_num_threads(args.threads)
    torch.manual_seed(SEED)
    rng = random.Random(SEED)

    destination = GENERATOR_STAGE1 if args.stage == 1 else GENERATOR
    start_from = BASE_MODEL if args.stage == 1 else str(GENERATOR_STAGE1)
    if args.stage == 2 and not (GENERATOR_STAGE1 / "config.json").exists():
        raise SystemExit("stage 1 has not been trained yet; run --stage 1 first")

    # Hours of CPU training must not depend on nothing interrupting it. The
    # checkpoint is written every few minutes and --resume carries on from it,
    # so the most an interruption can cost is those few minutes.
    resumed_from = None
    if args.resume and (destination / "config.json").exists():
        start_from = str(destination)
        resumed_from = str(destination)
        print(f"resuming from {destination}")

    print(f"loading {start_from}")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = T5ForConditionalGeneration.from_pretrained(start_from)
    model.train()

    train_rows, dev_rows = _rows_for_stage(args.stage, args.limit or None)
    if args.balance:
        before = len(train_rows)
        train_rows = balance(train_rows, args.balance, rng)
        print(f"balanced {before} -> {len(train_rows)} rows, "
              f"{args.balance} per question shape")
    train = build_examples(train_rows, args.stage, rng)
    dev = build_examples(dev_rows, args.stage, rng)
    print(f"stage {args.stage}: {len(train)} training examples, {len(dev)} dev")
    print("example input:\n  " + train[0][0][:300])
    print("example target:\n  " + train[0][1][:200])

    def collate(batch):
        inputs = tokenizer([b[0] for b in batch], max_length=MAX_INPUT,
                           truncation=True, padding=True, return_tensors="pt")
        targets = tokenizer([b[1] for b in batch], max_length=MAX_TARGET,
                            truncation=True, padding=True, return_tensors="pt")
        labels = targets.input_ids
        # -100 is what the loss ignores: padding must not be learned.
        labels[labels == tokenizer.pad_token_id] = -100
        inputs["labels"] = labels
        return inputs

    class Buckets(torch.utils.data.Sampler):
        """Batch examples of similar length together.

        Padding is per batch, so one 380-token schema in a batch of short
        questions makes every other row in it 380 tokens long -- and on a CPU
        that padding is most of the arithmetic. Sorting by length first, then
        shuffling the ORDER of the batches, keeps the randomness that matters
        for training and drops the padding that does not.
        """

        def __init__(self, examples, batch_size: int, seed: int):
            lengths = [len(tokenizer(text, truncation=True,
                                     max_length=MAX_INPUT).input_ids)
                       for text, _ in examples]
            order = sorted(range(len(examples)), key=lambda i: lengths[i])
            self.batches = [order[i:i + batch_size]
                            for i in range(0, len(order), batch_size)]
            self.rng = random.Random(seed)

        def __iter__(self):
            self.rng.shuffle(self.batches)
            return iter(self.batches)

        def __len__(self):
            return len(self.batches)

    loader = DataLoader(train, batch_sampler=Buckets(train, args.batch, SEED),
                        collate_fn=collate)
    dev_loader = DataLoader(dev, batch_size=args.batch, collate_fn=collate)

    steps_per_epoch = math.ceil(len(loader) / args.accumulate)
    total_steps = max(1, int(steps_per_epoch * args.epochs))
    optimiser = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    schedule = get_linear_schedule_with_warmup(
        optimiser, num_warmup_steps=int(0.06 * total_steps),
        num_training_steps=total_steps,
    )

    print(f"{total_steps} optimiser steps "
          f"(batch {args.batch} x {args.accumulate}, lr {args.lr})")

    history: list[dict] = []
    started = time.monotonic()
    step = 0
    done = False

    for epoch in range(math.ceil(args.epochs)):
        running = 0.0
        counted = 0
        for index, batch in enumerate(loader):
            loss = model(**batch).loss / args.accumulate
            loss.backward()
            running += loss.item() * args.accumulate
            counted += 1

            if (index + 1) % args.accumulate == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimiser.step()
                schedule.step()
                optimiser.zero_grad()
                step += 1

                if args.save_every and step % args.save_every == 0:
                    destination.mkdir(parents=True, exist_ok=True)
                    model.save_pretrained(destination)
                    tokenizer.save_pretrained(destination)

                if step % 25 == 0:
                    elapsed = time.monotonic() - started
                    rate = step / elapsed
                    left = (total_steps - step) / rate if rate else 0
                    print(f"  step {step}/{total_steps} "
                          f"loss {running / max(1, counted):.4f} "
                          f"{rate * 60:.1f} steps/min "
                          f"eta {left / 60:.0f} min", flush=True)
                    running, counted = 0.0, 0

                if step >= total_steps:
                    done = True
                    break

        # Dev loss each epoch: the number that says whether another epoch is
        # worth its forty minutes.
        model.eval()
        with torch.no_grad():
            losses = [model(**batch).loss.item() for batch in dev_loader]
        model.train()
        dev_loss = sum(losses) / max(1, len(losses))
        history.append({"epoch": epoch + 1, "step": step, "dev_loss": dev_loss,
                        "minutes": round((time.monotonic() - started) / 60, 1)})
        print(f"epoch {epoch + 1}: dev loss {dev_loss:.4f} "
              f"after {history[-1]['minutes']:.1f} min", flush=True)

        if done:
            break

    destination.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(destination)
    tokenizer.save_pretrained(destination)

    record = {
        "stage": args.stage, "base_model": BASE_MODEL, "started_from": start_from,
        "resumed_from": resumed_from,
        "examples": len(train), "epochs": args.epochs, "batch": args.batch,
        "accumulate": args.accumulate, "lr": args.lr, "steps": step,
        "max_input": MAX_INPUT, "max_target": MAX_TARGET,
        "history": history, "minutes": round((time.monotonic() - started) / 60, 1),
        "checkpoint": str(destination),
    }
    (RESULTS / f"generator-stage{args.stage}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8")
    print(f"saved {destination} in {record['minutes']} minutes")


if __name__ == "__main__":
    sys.exit(main())
