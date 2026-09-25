"""What Model B is actually worth (Backend Plan §23, weeks 5-7).

Three numbers per model, because each hides something the others catch:

    execution accuracy   both statements were RUN on the seeded warehouse and
                         returned the same rows. The number that matters.
    exact match          the statement matches the gold one, canonicalised.
                         Cheap, and unfair: there are many correct ways to
                         write the same query and this calls them wrong.
    validator pass       it got past layer 2 at all. A generator the validator
                         refuses is not a safety problem -- refusal works --
                         but it is a useless one.

And two slices of the test set, never averaged together:

    new phrasing   a wording the model never saw, of a shape it did
    new shape      a question type held out of training entirely

Reporting the first alone is reporting memorisation with extra steps.

    python -m ml.generator_eval              CodeT5, plus a Gemma sample
    python -m ml.generator_eval --gemma 0    CodeT5 only (Gemma is slow on CPU)

Gemma is sampled rather than run over the whole set: on the demo machine it
takes tens of seconds a question. The sample size is recorded beside the
number so the comparison is not quoted as more than it is.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time
from decimal import Decimal

import sqlglot
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from core.validator import Permitted, validate
from ml.paths import GENERATOR, RESULTS, SYNTH, read_jsonl
from ml.serialize import columns_for, context_object, choose_tables
from ml.synth import warehouse_dsn
from ml.warehouse import warehouse_schema

SEED = 20260923
MAX_ROWS = 500


def canonical(sql: str) -> str | None:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        return normalize_identifiers(tree, dialect="postgres").sql(dialect="postgres")
    except Exception:  # noqa: BLE001 - unparseable output is simply not a match
        return None


def _value(value):
    """482140.00 and 482140 are the same answer; dates compare as text."""
    if isinstance(value, Decimal):
        return round(float(value), 4)
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (int, str)) or value is None:
        return value
    return str(value)


def _run(conn, sql: str):
    from sqlalchemy import text  # noqa: PLC0415

    rows = conn.execute(text(sql)).fetchmany(MAX_ROWS)
    return [tuple(_value(v) for v in row) for row in rows]


def same_result(conn, gold: str, predicted: str) -> bool | None:
    """True, False, or None when the GOLD statement is the one that failed."""
    try:
        expected = _run(conn, gold)
    except Exception:  # noqa: BLE001
        conn.rollback()
        return None
    try:
        actual = _run(conn, predicted)
    except Exception:  # noqa: BLE001 - the usual failure: an invented column
        conn.rollback()
        return False

    # Row order counts only when the gold statement asked for one.
    if "order by" in gold.lower():
        return expected == actual
    return sorted(expected, key=repr) == sorted(actual, key=repr)


def evaluate(rows: list[dict], generate, label: str, conn,
             permitted: Permitted) -> dict:
    result = {"model": label, "n": len(rows), "exact": 0, "execution": 0,
              "validator": 0, "unrunnable_gold": 0, "errors": 0, "by_kind": {},
              "examples": []}
    started = time.monotonic()

    for row in rows:
        try:
            predicted = generate(row["question"], row["_schema"])
        except Exception as exc:  # noqa: BLE001
            # A model that errors did not answer, which is a wrong answer and
            # counted as one. The first version raised here, so one HTTP 500
            # from a loaded-down Ollama threw away a twenty-minute run.
            result["errors"] += 1
            predicted = ""
            if len(result["examples"]) < 8:
                result["examples"].append({
                    "question": row["question"], "kind": row["kind"],
                    "gold": row["sql"], "predicted": f"[error] {exc}"[:200],
                })

        kind = result["by_kind"].setdefault(
            row["kind"], {"n": 0, "exact": 0, "execution": 0, "validator": 0})
        kind["n"] += 1

        if validate(predicted, permitted).allowed:
            result["validator"] += 1
            kind["validator"] += 1

        predicted_canonical = canonical(predicted)
        if predicted_canonical and predicted_canonical == canonical(row["sql"]):
            result["exact"] += 1
            kind["exact"] += 1

        verdict = same_result(conn, row["sql"], predicted)
        if verdict is None:
            result["unrunnable_gold"] += 1
        elif verdict:
            result["execution"] += 1
            kind["execution"] += 1
        elif len(result["examples"]) < 8:
            result["examples"].append({
                "question": row["question"], "kind": row["kind"],
                "gold": row["sql"], "predicted": predicted,
            })

    result["seconds"] = round(time.monotonic() - started, 1)
    result["ms_per_question"] = round(1000 * result["seconds"] / max(1, len(rows)))
    for key in ("exact", "execution", "validator"):
        result[f"{key}_pct"] = round(100 * result[key] / max(1, len(rows)), 1)
    for kind in result["by_kind"].values():
        for key in ("exact", "execution", "validator"):
            kind[f"{key}_pct"] = round(100 * kind[key] / max(1, kind["n"]), 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemma", type=int, default=40,
                        help="how many questions to also put to Gemma (0 skips)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--checkpoint", default=str(GENERATOR))
    parser.add_argument("--label", default="codet5-small (Spider then warehouse)")
    parser.add_argument("--router", action="store_true",
                        help="also measure what the PRODUCT serves: Model B when "
                             "its confidence clears the threshold, Gemma below it")
    parser.add_argument("--threshold", type=float, default=0.55)
    parser.add_argument("--out", default="generator-accuracy.json",
                        help="file in ml/results to write -- use a second name "
                             "to keep a stage-1-only run beside the final one")
    args = parser.parse_args()

    from sqlalchemy import create_engine  # noqa: PLC0415

    from core.sql_generator import CodeT5Generator, GemmaGenerator  # noqa: PLC0415

    rng = random.Random(SEED)
    tables, foreign_keys = warehouse_schema()
    columns = columns_for(tables, foreign_keys)
    permitted = Permitted(frozenset(f"public.{t}" for t in tables))

    rows = list(read_jsonl(SYNTH / "test.jsonl"))
    if args.limit:
        rows = rows[: args.limit]
    for row in rows:
        # The schema exactly as the route would hand it over.
        row["_schema"] = context_object(columns, choose_tables(columns, row["tables"], rng))

    engine = create_engine(warehouse_dsn(), future=True)
    everything: dict[str, dict] = {}

    with engine.connect() as conn:
        codet5 = CodeT5Generator(args.checkpoint)
        if codet5.available():
            everything["codet5-small"] = evaluate(
                rows, lambda q, s: codet5.generate(q, s).sql,
                args.label, conn, permitted)
        else:
            print(f"no generator checkpoint at {args.checkpoint}; train it first")

        if args.gemma:
            from core.llm_client import LLMClient  # noqa: PLC0415

            client = LLMClient(
                mode="local", model=os.environ.get("LLM_MODEL", "gemma3:4b"),
                endpoint=os.environ.get("LLM_ENDPOINT", "http://localhost:11434"),
            )
            if client.health():
                gemma = GemmaGenerator(client)
                sample = rng.sample(rows, min(args.gemma, len(rows)))
                everything["gemma"] = evaluate(
                    sample, lambda q, s: gemma.generate(q, s).sql,
                    f"{client.model} (off-the-shelf, sample of {len(sample)})",
                    conn, permitted)

                if args.router and codet5.available():
                    # The number that describes the PRODUCT rather than either
                    # model: Model B answers, and only when its own confidence
                    # falls below the threshold does the question cost a Gemma
                    # call. How often that happens is recorded beside it,
                    # because "how much is served locally" is half the claim.
                    served = {"local": 0, "escalated": 0}

                    def routed(question, schema):
                        candidate = codet5.generate(question, schema)
                        if candidate.confidence >= args.threshold and candidate.sql:
                            served["local"] += 1
                            return candidate.sql
                        served["escalated"] += 1
                        return gemma.generate(question, schema).sql

                    everything["router"] = evaluate(
                        sample, routed,
                        f"the router: codet5-small, Gemma below {args.threshold} "
                        f"(sample of {len(sample)})", conn, permitted)
                    everything["router"]["served"] = served
            else:
                print("Gemma is not reachable; skipping that row")

    engine.dispose()

    for result in everything.values():
        print(f"\n{result['model']}  "
              f"({result['n']} questions, {result['ms_per_question']} ms each)")
        print(f"  execution accuracy  {result['execution_pct']:5.1f}%")
        print(f"  exact match         {result['exact_pct']:5.1f}%")
        print(f"  passes validator    {result['validator_pct']:5.1f}%")
        if result.get("errors"):
            print(f"  the model errored   {result['errors']} of {result['n']} "
                  "(counted as wrong)")
        for kind, scores in sorted(result["by_kind"].items()):
            print(f"    {kind:<14} n={scores['n']:<4} "
                  f"execution {scores['execution_pct']:5.1f}%  "
                  f"exact {scores['exact_pct']:5.1f}%")
        if "served" in result:
            served = result["served"]
            total = max(1, served["local"] + served["escalated"])
            print(f"  served locally      {100 * served['local'] / total:5.1f}% "
                  f"({served['local']} of {total})")

    if everything:
        (RESULTS / args.out).write_text(
            json.dumps(everything, indent=2), encoding="utf-8")
        print(f"\nwritten to {RESULTS / args.out}")


if __name__ == "__main__":
    main()
