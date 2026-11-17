"""Where the data and the checkpoints live. One definition, imported by all."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Iterator

# transformers imports TensorFlow if it can find it, and a machine with Keras 3
# installed for something else then fails on import. This project is torch-only.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")

ML = Path(__file__).resolve().parent
BACKEND = ML.parent

DATA = ML / "data"
SPIDER = DATA / "spider"          # downloaded; gitignored
SYNTH = DATA / "synth"            # generated and committed: it is a deliverable
CHECKPOINTS = ML / "checkpoints"  # gitignored
RESULTS = ML / "results"          # the measured tables; committed

RETRIEVER = CHECKPOINTS / "retriever"
GENERATOR = CHECKPOINTS / "generator"
GENERATOR_STAGE1 = CHECKPOINTS / "generator-stage1"

for directory in (DATA, SPIDER, SYNTH, CHECKPOINTS, RESULTS):
    directory.mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
