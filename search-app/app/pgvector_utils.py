from __future__ import annotations

from typing import Sequence


def to_vec_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(format(float(x), ".8f") for x in vec) + "]"
