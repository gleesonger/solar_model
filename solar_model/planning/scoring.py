"""Current cost/battery objective; protection-score design remains proposed."""
from collections.abc import Sequence

import cvxpy as cp

from solar_model.config import OptimisationWeightsConfig


def total_score(net_cost_flows: Sequence[cp.Expression], battery_level_worth: Sequence[cp.Expression],
                scenario_count: int, weights: OptimisationWeightsConfig) -> cp.Expression:
    """Average scenario costs and tariff-valued battery levels using configured weights."""
    return (-cp.sum(net_cost_flows) / scenario_count * weights.maximise_profit
            + cp.sum(battery_level_worth) / scenario_count * weights.maximise_battery)
