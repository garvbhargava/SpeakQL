"""The recall table for Model A (Backend Plan §23, week 2).

Three retrievers, the same questions, the same metric:

    lexical              token overlap. No model. The off-the-shelf baseline
                         the comparative study needs a row for.
    MiniLM off-the-shelf all-MiniLM-L6-v2 as published
    MiniLM fine-tuned    the same model after ml/retriever_train.py

The metric is **strict recall@k: every gold table inside the top k**, not
"at least one". The generator is handed the top k and nothing else, so a
query needing three tables of which two were retrieved is a query that cannot
be written. Counting that as two-thirds right would flatter the number.

Two sets, because they answer different questions:

    Spider dev    20 databases never seen in training -- does it generalise
                  to a schema it has never read?
    warehouse     the synthetic test pairs -- does it work on the six tables
                  the demo actually asks about?

    python -m ml.retriever_eval
"""

from __future__ import annotations

import json
import time

from ml.paths import RESULTS, RETRIEVER, SPIDER, SYNTH, read_jsonl
from core.schema_retriever import table_document
from ml.warehouse import warehouse_schema

KS = (1, 3, 5)


def _spider_cases(limit: int | None = None) -> list[dict]:
    cases = []
    for row in read_jsonl(SPIDER / "dev.jsonl"):
        cases.append({
            "question": row["question"],
            "gold": [t for t in row["tables"] if t in row["schema"]["tables"]],
            "tables": row["schema"]["tables"],
        })
        if limit and len(cases) >= limit:
            break
    return [c for c in cases if c["gold"]]


def _warehouse_cases() -> list[dict]:
    tables, _ = warehouse_schema()
    return [
        {"question": row["question"],
         "gold": [t for t in row["tables"] if t in tables],
         "tables": tables}
        for row in read_jsonl(SYNTH / "test.jsonl")
        if [t for t in row["tables"] if t in tables]
    ]


# ------------------------------------------------------------- retrievers ---

def _lexical_ranking(case: dict) -> list[str]:
    from core.schema_retriever import Column, LexicalRetriever  # noqa: PLC0415

    columns = [Column("public", table, column, "text")
               for table, names in case["tables"].items() for column in names]
    scored = LexicalRetriever().search(case["question"], columns,
                                       k=len(case["tables"]))
    return [s.table.split(".")[-1] for s in scored]


def _embedding_ranking(model, cases: list[dict]) -> list[list[str]]:
    from sentence_transformers import util  # noqa: PLC0415

    questions = model.encode([c["question"] for c in cases],
                             convert_to_tensor=True, normalize_embeddings=True,
                             batch_size=64, show_progress_bar=False)

    rankings: list[list[str]] = []
    cache: dict[tuple[str, ...], tuple[list[str], object]] = {}
    for index, case in enumerate(cases):
        key = tuple(sorted(case["tables"]))
        if key not in cache:
            names = sorted(case["tables"])
            documents = [table_document(n, case["tables"][n]) for n in names]
            cache[key] = (names, model.encode(documents, convert_to_tensor=True,
                                              normalize_embeddings=True,
                                              show_progress_bar=False))
        names, matrix = cache[key]
        scores = util.cos_sim(questions[index], matrix)[0]
        order = scores.argsort(descending=True).tolist()
        rankings.append([names[i] for i in order])
    return rankings


def recall_at_k(rankings: list[list[str]], cases: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for k in KS:
        covered = sum(
            1 for ranking, case in zip(rankings, cases)
            if set(case["gold"]) <= set(ranking[:k])
        )
        out[f"recall@{k}"] = round(covered / max(1, len(cases)), 4)
    return out


def evaluate(name: str, cases: list[dict]) -> dict[str, dict]:
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    results: dict[str, dict] = {}

    started = time.monotonic()
    results["lexical"] = recall_at_k([_lexical_ranking(c) for c in cases], cases)
    results["lexical"]["seconds"] = round(time.monotonic() - started, 1)

    for label, path in (("minilm-off-the-shelf", "sentence-transformers/all-MiniLM-L6-v2"),
                        ("minilm-fine-tuned", str(RETRIEVER))):
        if label == "minilm-fine-tuned" and not RETRIEVER.exists():
            print(f"  {label}: not trained yet, skipped")
            continue
        started = time.monotonic()
        model = SentenceTransformer(path)
        rankings = _embedding_ranking(model, cases)
        results[label] = recall_at_k(rankings, cases)
        results[label]["seconds"] = round(time.monotonic() - started, 1)

    print(f"\n{name}: {len(cases)} questions")
    header = "  {:<22}" + "".join(f"  {'recall@' + str(k):>10}" for k in KS)
    print(header.format("retriever"))
    for label, scores in results.items():
        row = f"  {label:<22}" + "".join(
            f"  {scores[f'recall@{k}'] * 100:9.1f}%" for k in KS)
        print(row)
    return results


def main() -> None:
    everything = {
        "spider-dev": evaluate("Spider dev (unseen databases)", _spider_cases()),
        "warehouse-test": evaluate("Warehouse test pairs", _warehouse_cases()),
    }
    (RESULTS / "retriever-recall.json").write_text(
        json.dumps(everything, indent=2), encoding="utf-8")
    print(f"\nwritten to {RESULTS / 'retriever-recall.json'}")


if __name__ == "__main__":
    main()
