# DP loop optimisation experiment

Measured 2026-10-31 using isolated Python prototypes compiled through the existing Numba switch. Caching plus command pruning was subsequently applied to the production optimiser; reversed loops were not adopted. See the [LLVM inspection](dp-numba-llvm-review.md) for the observations that motivated it.

## Changes tested

1. **Cache repeated lookups:** fetch the next period's grid and value row once per period, and cache interpolation endpoints in local variables. This avoids repeated typed-list access inside command evaluation.
2. **Reverse backward loops:** visit commands before battery nodes, rather than battery nodes before commands. Each node still sees commands in the original order; this changes traversal without reducing the number of simulations.
3. **Cache and prune commands:** retain change 1 and skip higher rates of a mode after a feasible simulation shows that requested power is already capped. Also skip higher charge rates after moving above the next grid's upper bound, or higher discharge rates after moving below its lower bound. Increasing that rate cannot return to the corridor. Evaluate the threshold command itself normally. Separate charge/discharge thresholds preserve command order and first-winner tie handling, including when commands are shuffled.

No explicit inlining, fast-math, parallel execution, grid changes or scoring changes were used. The pruning prototype adds the same short check to backward valuation and forward projection.

## Timing

The configured synthetic baseline has nine scenarios, 96 visible 15-minute periods and six hidden four-hour periods. Refinements are `(2% /5 unrestricted) -> (1% /10 +/-5%) -> (1% /20 +/-3%) -> (1% /100 +/-1%)`.

Each variant used a reusable worker. Its first call was discarded; three measured calls were paired with a baseline immediately before each call, rotating variant order between rounds. All simulations had a 120-second cutoff; none timed out. The mean paired runtime reduction below averages the three individual ratios. Runtime covers the complete optimisation including reports, excluding the benchmark's independent validation.

| Python change | Paired baseline mean | Variant mean | Runtime reduction |
| --- | ---: | ---: | ---: |
| Cache repeated lookups | 2.774 s | 2.100 s | 24.2% |
| Reverse backward loops | 2.735 s | 2.809 s | -2.7% |
| Cache and prune commands | 2.784 s | 1.535 s | 44.9% |

For caching plus pruning, individual reductions were 43.6-45.8%. Mean backward time was 0.437 s and forward time 0.456 s, versus 0.735 s and 1.373 s across the nine measured baseline controls. The phase baseline averages use all controls, whereas each total-runtime comparison above uses only its three paired controls.

The machine was unplugged on the Balanced power plan (71% battery when checked). Absolute timings differ from the earlier [current-model comparison](current-models-performance.md); paired controls help account for changing conditions but three pairs are not a general performance guarantee. An initial unpaired batch had a large late slowdown and was excluded from these conclusions.

## Output checks and conclusion

All 22 paired-batch calls passed the benchmark's physical checks and exactly matched baseline weighted score, costs, shared first command and all nine scenario summaries. Weighted full score was `5.412220603316844`, visible cost EUR `0.43657637520477105`, full cost EUR `0.3111073981027862`; the shared first command was self-consumption.

A separate 70-case seeded check compared every backward table value and sampled complete forward paths against the original Python functions, with exact equality. Cases varied forecasts, tariffs, export limits, corridors, command divisions and command order. This is useful equivalence evidence for the prototype, rather than a proof across all possible inputs.

Reversing traversal alone did not help. Removing repeated row lookups helped substantially; avoiding simulations of commands whose result is already capped or farther outside the corridor provided the largest additional gain. Production now uses caching plus pruning in the shared Python/Numba functions. Targeted regression coverage compares backward values and forward paths against exhaustive command evaluation and checks that fewer simulations are performed, including reordered commands, physical caps, export restrictions and bounded grids. Existing Python/Numba equivalence checks cover the compiled path. The timing table remains the prototype measurement, not a new production benchmark.

Local reproducibility artifacts are under the ignored `documentation/reviews/system-review-evidence/loop-experiments/` folder: `lookup_cache.py`, `reverse_backward.py`, `pruned.py`, `validate_variants.py`, `run_paired.py`, `loop-variants-config.yaml` and `loop-variants-paired.json`. The JSON records source hashes, settings, per-call timings and validation summaries; the runner verified that source snapshots remained unchanged.
