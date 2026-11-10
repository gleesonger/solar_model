"""Validated duration conversion for configured three-condition transitions."""
from collections.abc import Sequence
from functools import lru_cache
import math


DEFAULT_TRANSITION_MATRIX = ((.6, .3, .1), (.2, .6, .2), (.1, .3, .6))


def validate_transition_inputs(matrix: Sequence[Sequence[float]], reference_hours: float) -> None:
    if isinstance(reference_hours, bool) or not math.isfinite(reference_hours) or reference_hours <= 0:
        raise ValueError("dynamic_programming.transition_hours must be finite and positive")
    if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
        raise ValueError("dynamic_programming.transition_matrix must have three rows and three columns in low/med/high order")
    for row in matrix:
        if any(isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1 for value in row):
            raise ValueError("dynamic_programming.transition_matrix probabilities must be finite and between zero and one")
        if not math.isclose(sum(row), 1., abs_tol=1e-10, rel_tol=0.):
            raise ValueError("dynamic_programming.transition_matrix rows must sum to one")


@lru_cache(maxsize=128)
def transition(hours: float, matrix: tuple[tuple[float, ...], ...] = DEFAULT_TRANSITION_MATRIX, reference_hours: float = 3.) -> tuple[tuple[float, ...], ...]:
    """Scale each row's exit probability, retaining destination proportions."""
    if isinstance(hours, bool) or not math.isfinite(hours) or hours < 0:
        raise ValueError("transition duration must be finite and non-negative")
    validate_transition_inputs(matrix, reference_hours)
    result = []
    for i, row in enumerate(matrix):
        exit_probability = sum(row[j] for j in range(3) if j != i)
        scaled_exit = 0.
        if hours > 0 and exit_probability > 0:
            # log1p/expm1 preserve precision for small probabilities/durations.
            scaled_exit = 1. if exit_probability >= 1. else -math.expm1(math.log1p(-exit_probability) * (hours / reference_hours))
        values = [scaled_exit * row[j] / exit_probability if j != i and exit_probability > 0 else 0. for j in range(3)]
        values[i] = 1. - scaled_exit
        result.append(tuple(values))
    return tuple(result)
