# Dynamic programming

Markov DP is the only energy-plan optimiser. The MILP and independent-scenario DP implementations, selectors, staged corridors and solver dependencies have been removed. See the [Markov implementation](markov-dynamic-programming.md) for the recurrence, information assumptions, transitions, expected reporting and stress-case contracts.

The public entry point is `planning.plan_optimisation_dynamic_programming.simulate_energy_plan()`. It prepares UTC-aligned periods, tariffs, schedules, physical limits and the complete battery grid, then calls `markov.solve_prepared()`. Python and Numba run the same numerical functions. Compilation remains lazy. Logs separate model-kernel compilation from Numba support compilation and report model cache hits/misses; see [native compilation diagnostics](markov-dynamic-programming.md#python-and-numba).

Required settings live directly in `energy_plan.optimiser`: `compile_model_to_native_code`, `transition_hours`, `transition_matrix` (separate required `solar` and `house_load` matrices), `projection_resolution_mins`, `grid_step_percent` and `power_divisions`. There is one full-range solve. No model selector, uncertainty selector, nested solver blocks or refinement stages remain. Missing values fail validation; legacy keys are rejected, not migrated or defaulted.

`grid_step_percent` spaces regular battery-energy nodes as a percentage of capacity; exact initial energy and schedule limits are also included. `power_divisions` divides the configured minimum-to-effective-maximum power range for each forced policy. Zero-power variants and self-consumption remain candidates. For example, five divisions give five positive rates per forced policy, not five-percent increments.

The first action lasts fifteen minutes. Remaining steps follow configured durations and UTC clock alignment, split at the visible cutoff, and finish at the total elapsed horizon. The timestep list must explicitly cover the entire horizon. Neither tariffs nor midnight create additional splits.

Physics, command profiles, route allocation, nonlinear protection, soft user battery bounds, hard hardware/grid limits and terminal credit are shared throughout the Markov solve. Fixed-condition reports illustrate stress outcomes of its policy; their probability-weighted average is not the stochastic expectation. Saved historical projections remain displayable without rerunning their retired solver.
