# DP Numba LLVM inspection

Inspected 2026-10-31 using Numba 0.68.1, llvmlite 0.50.0 and LLVM 22.1.0 on Windows x86-64. The numerical model was not changed. This is a code-generation review, not a runtime benchmark or a measured speedup.

Source SHA-256 for `plan_optimisation_dynamic_programming.py`: `2b7bcb0d118fd878d827210bc24bdccf1d9dff948c19683f23c168f182ae93aa`.

## Capture

Used `get_numba_functions()`, real typed-list/named-tuple inputs, `inspect_llvm(signature)`, `inspect_asm(signature)` and `inspect_types()`. The representative inputs used two periods, float battery grids, three operating modes, the same thirteen-field `Period` including epoch floats and tariff strings, and the six-field device record. Container lengths differ from a full projection but their element types and compiled signatures match the numerical model's normal types.

A separate inspection cache was used to obtain generated IR without clearing the application's cache. The full modules contain 2,213 lines for backward calculation and 1,761 for forward projection, including wrappers and native helpers. The observed optimisation setting was level 3.

Local dumps are under ignored `documentation/reviews/system-review-evidence/numba-ir/`, with copies in `%TEMP%/solar-model-numba-llvm/`. Full modules are `backward.ll` and `forward.ll`; `*.native.ll` extracts remove the Python wrappers. Assembly, type listings, static call-site summaries and loop-block membership are included. Static instruction and call-site counts do not measure how frequently a path executes or its runtime cost.

## Findings

### The transition loops are native, and named-tuple fields are scalar values

The native backward/forward functions call native helpers and runtime support, rather than Python flow-simulation callbacks. Python object conversion appears in their separate CPython wrappers. The single-period simulator has no heap-allocation calls: its eight-number `Step` result is written into a native return structure. There is no Python `Step` allocation for every candidate transition.

Returning a completed forward path does allocate Python objects: the wrapper contains `PyList_New`, eight `PyFloat_FromDouble` call sites and `PyObject_Call` to reconstruct each `Step`. This is per returned path element, not per candidate evaluation. It is a possible later batching optimisation, not evidence of object-mode execution.

### Simulator and interpolation calls remain inside the command loop

The native forward loop still calls `simulate_system_single_time_period` and `interpolate` separately. The simulator's native interface has 31 arguments after tuple expansion, including return/exception pointers. It also writes eight doubles that the caller loads from its return buffer. These are native calls, but their argument/return handling is repeated for candidate evaluations.

Four fixed power-limit multiplications remain inside the simulator:

```llvm
%output = fmul double %device_output_kw, %period_hours
%absorption = fmul double %device_absorption_kw, %period_hours
%charge_limit = fmul double %device_charge_kw, %period_hours
%discharge_limit = fmul double %device_discharge_kw, %period_hours
```

The excerpt uses readable operand names in place of generated identifiers. These quantities are unchanged across a period's candidate commands, but the current out-of-line call recomputes them on each invocation. Battery-protection arithmetic is already inlined into this simulator, so that smaller helper does not have a separate native call here.

### Typed-list access carries runtime bookkeeping

The native interpolation routine repeatedly calls `numba_list_size_address`, `numba_list_base_ptr`, `NRT_incref` and `NRT_decref`. The binary-search loop itself contains a length fetch, base-pointer fetch, element load and reference-count handling for a probe. Endpoint values are also read repeatedly for feasibility checks and the final interpolation expression.

The assembly for the NRT reference-count helpers contains `lock incq` and `lock decq`, guarded for null references. These are atomic bookkeeping operations, rather than simple scalar loads. This is a concrete reason to investigate caching row references and neighbour values before assuming that arithmetic or Python/native crossings dominate.

In forward projection, the next grid and future-value rows are fetched from their outer typed lists inside the feasible-command loop. Both are fixed for the entire period and can instead be fetched once outside that loop. Backward calculation has the same opportunity for its next grid and last calculated value row.

### An unused tariff string still incurs reference handling in the simulator

The simulator never uses `period.tariff_name`, but its expanded period argument includes that string. Its body emits an `NRT_incref` on the string's memory-info pointer and matching decrefs on normal/infeasible exits. Thus unused descriptive data can still carry ownership bookkeeping when the whole named tuple crosses a native helper boundary.

This does not justify reintroducing a separate period-input record. Inlining can remove that helper boundary while retaining the current single `Period` type.

## Isolated inlining experiment

An isolated source copy changed only the helper registration policy:

```python
register_jitable(inline="always")(helper)
```

Production source was not edited. The original source and the copy were checked to match after reversing that one replacement. Both functions were compiled and inspected with the same representative input types. [Numba documents this control](https://numba.readthedocs.io/en/stable/developer/inlining.html) as Numba-IR inlining, distinct from LLVM's own inlining; it is not a general promise of improved speed.

Observed changes in the generated LLVM:

- Separate simulator and interpolation native call sites disappeared from the candidate loops.
- All four fixed device-power-by-duration multiplications moved outside the candidate-command loop in forward projection. The corresponding backward multiplications also moved outside the command/state loops to period-level preparation or its preheaders.
- The unused tariff string's ownership remained at the period iteration rather than crossing a separate simulator boundary per candidate.
- Typed-list lookup/reference-count operations inside interpolation remained; inlining alone did not turn those accesses into plain array indexing.

The isolated variant's full modules have 2,151 backward and 1,658 forward lines. These smaller module totals are not a speed measurement; the main native function bodies grew as helper bodies were incorporated. Compilation cost, instruction-cache effects and actual runtime require a controlled comparison. No full-model performance benchmark was run for this experiment.

## Recommended order of experiments

1. Benchmark selective simulator/interpolation inlining while keeping the named tuples and existing functions. This is a small compilation-policy change, and its code-generation effects are already visible.
2. Fetch next-period grid/value rows once per period and reuse interpolation neighbour values locally. These are small source changes aimed at the emitted list bookkeeping.
3. If list overhead remains material, evaluate contiguous numerical grid/value rows or batched rollout scoring. Those require more structural change and should follow the smaller experiments.

Keep strict floating-point behaviour: infeasibility uses `-inf` and explicit finite checks. General `fastmath=True` should not be applied as an unverified speed switch. Threading also needs measurement: concurrent candidate rollouts would share some reference-counted inputs, so atomic bookkeeping can affect scaling.
