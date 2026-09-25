"""Model A: a MiniLM bi-encoder that finds the right tables (weeks 1-3).

The generator never sees the whole schema. It sees the tables this model
retrieved -- so a table missed here cannot be recovered later, whatever the
generator does. On an 18-table warehouse that is a convenience; on a
registered database with two hundred tables it is the difference between a
prompt that fits and one that does not.

**Bi-encoder, not cross-encoder.** Each table is embedded once, in advance;
a question is one embedding and then a vector search. A cross-encoder would
score the question against every table on every request, which is a model call
per table per question -- accurate and unusable.

Training is contrastive with in-batch negatives (MultipleNegativesRankingLoss):
for each (question, its gold table) pair, every other table in the batch is a
negative. One hard negative is added per example -- a table from the SAME
database that the query does not use -- because the easy negatives from other
databases teach almost nothing.

    python -m ml.retriever_train --epochs 1

Evaluation is a separate script, so the recall table cannot be quietly
produced by the code that did the training.
"""

from __future__ import annotations

import argparse
import json
import random
import time

from ml.paths import RESULTS, RETRIEVER, SPIDER, SYNTH, read_jsonl
from core.schema_retriever import table_document
from ml.warehouse import warehouse_schema

BASE_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SEED = 20260923


def spider_examples(rng: random.Random, limit: int | None = None) -> list[tuple[str, str, str]]:
    """(question, gold table document, hard negative document)."""
    examples: list[tuple[str, str, str]] = []
    for row in read_jsonl(SPIDER / "train.jsonl"):
        tables = row["schema"]["tables"]
        others = [t for t in tables if t not in row["tables"]]
        for gold in row["tables"]:
            if gold not in tables:
                continue
            negative = rng.choice(others) if others else None
            examples.append((
                row["question"],
                table_document(gold, tables[gold]),
                table_document(negative, tables[negative]) if negative else "",
            ))
        if limit and len(examples) >= limit:
            break
    return examples


def warehouse_examples(rng: random.Random) -> list[tuple[str, str, str]]:
    """The same, over this warehouse: the questions it will actually be asked."""
    tables, _ = warehouse_schema()
    examples: list[tuple[str, str, str]] = []
    for row in read_jsonl(SYNTH / "train.jsonl"):
        others = [t for t in tables if t not in row["tables"]]
        for gold in row["tables"]:
            if gold not in tables:
                continue
            negative = rng.choice(others) if others else None
            examples.append((
                row["question"],
                table_document(gold, tables[gold]),
                table_document(negative, tables[negative]) if negative else "",
            ))
    return examples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--spider-limit", type=int, default=0)
    args = parser.parse_args()

    from sentence_transformers import (  # noqa: PLC0415
        InputExample, SentenceTransformer, losses,
    )
    from torch.utils.data import DataLoader  # noqa: PLC0415

    rng = random.Random(SEED)
    spider = spider_examples(rng, args.spider_limit or None)
    warehouse = warehouse_examples(rng)
    print(f"{len(spider)} Spider pairs + {len(warehouse)} warehouse pairs")

    examples = [
        InputExample(texts=[question, positive, negative] if negative
                     else [question, positive])
        for question, positive, negative in spider + warehouse
    ]
    rng.shuffle(examples)

    model = SentenceTransformer(BASE_MODEL)
    loader = DataLoader(examples, shuffle=True, batch_size=args.batch,
                        drop_last=True)
    loss = losses.MultipleNegativesRankingLoss(model)

    started = time.monotonic()
    model.fit(
        train_objectives=[(loader, loss)],
        epochs=args.epochs,
        warmup_steps=int(0.06 * len(loader) * args.epochs),
        optimizer_params={"lr": args.lr},
        show_progress_bar=True,
        output_path=str(RETRIEVER),
    )
    minutes = round((time.monotonic() - started) / 60, 1)
    model.save(str(RETRIEVER))

    (RESULTS / "retriever-train.json").write_text(json.dumps({
        "base_model": BASE_MODEL, "spider_pairs": len(spider),
        "warehouse_pairs": len(warehouse), "epochs": args.epochs,
        "batch": args.batch, "lr": args.lr, "minutes": minutes,
        "checkpoint": str(RETRIEVER),
    }, indent=2), encoding="utf-8")
    print(f"saved {RETRIEVER} in {minutes} minutes")


if __name__ == "__main__":
    main()
