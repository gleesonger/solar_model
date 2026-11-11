"""Model cache reuse is distinct from process-local Numba support compilation."""
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from solar_model.planning.native_compilation import track_native_compilation


def test_python_execution_does_not_import_numba() -> None:
    script = "from solar_model.planning.native_compilation import track_native_compilation; import sys\nwith track_native_compilation(False) as metrics:\n assert metrics.cache == 'python'\nassert 'numba' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_native_diagnostics_do_not_warm_up_or_compile_a_kernel() -> None:
    script = """
from solar_model.planning.native_compilation import track_native_compilation
from solar_model.planning.markov import compiled_functions
with track_native_compilation(True) as metrics:
    assert all(not function.signatures for function in compiled_functions())
assert metrics.cache == 'unused'
assert metrics.model_seconds == metrics.support_seconds == 0.
assert metrics.disk_hits == metrics.disk_misses == 0
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_nested_compilation_is_not_double_counted(monkeypatch) -> None:
    from numba.core import event
    from solar_model.planning import markov

    functions = [SimpleNamespace(name=name, signatures=[], stats=SimpleNamespace(cache_hits=Counter(), cache_misses=Counter())) for name in ("backward", "forward")]
    monkeypatch.setattr(markov, "compiled_functions", lambda: functions)
    ticks = iter(range(20))
    monkeypatch.setattr(event, "timer", lambda: float(next(ticks)))
    with track_native_compilation(True) as metrics:
        with event.trigger_event("numba:compile", data={"dispatcher": functions[0]}):
            with event.trigger_event("numba:compile", data={"dispatcher": object()}):
                functions[0].stats.cache_misses["signature"] += 1
        with event.trigger_event("numba:compile", data={"dispatcher": object()}):
            pass
    assert metrics.model_seconds == 2.
    assert metrics.support_seconds == 1.
    assert metrics.cache == "compiled" and metrics.disk_misses == 1


def test_model_kernels_reuse_disk_cache_across_processes_and_memory_within_process(tmp_path) -> None:
    # Compile signatures only: this does not run a projection or benchmark.
    script = tmp_path / "compile_signatures.py"
    script.write_text('''
from dataclasses import asdict
import json
from numba import typeof
from numba.typed import List
from solar_model.planning import markov
from solar_model.planning import plan_optimisation_dynamic_programming as dp
from solar_model.planning.native_compilation import track_native_compilation

with track_native_compilation(True) as initial:
    period = dp.Period(0., 900., 0., .25, .4, .1, "flat", 0., 2., 999., .25, 0., 1.)
    periods = List([List([period])])
    transitions = List([List([List([1.])])])
    grid = List([0., 1., 2.])
    choices = List([dp.Action(dp.OperatingMode.SELF_CONSUMPTION, 0.)])
    policy = List([List([List([0, 0, 0])])])
    device = dp.DeviceParameters(1., 1., 1., 1., 4., 4., 4., 4., 4.)
    backward, forward = markov.compiled_functions()
    backward_signature = tuple(typeof(value) for value in (device, 2., periods, grid, choices, transitions, 0.))
    forward_signature = tuple(typeof(value) for value in (device, 2., periods, grid, choices, transitions, policy, 1, 0))
    backward.compile(backward_signature)
    forward.compile(forward_signature)
with track_native_compilation(True) as repeated:
    backward.compile(backward_signature)
    forward.compile(forward_signature)
print(json.dumps({"initial": asdict(initial), "repeated": asdict(repeated)}))
''', encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]), NUMBA_CACHE_DIR=str(tmp_path / "native-cache"))
    results = []
    for _ in range(2):
        result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=environment, timeout=90)
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout.strip().splitlines()[-1]))
    cold, warm = [result["initial"] for result in results]
    assert cold["cache"] == "compiled" and cold["disk_misses"] == 2
    assert warm["cache"] == "disk" and warm["disk_hits"] == 2 and warm["disk_misses"] == 0
    assert warm["model_seconds"] == 0. and warm["support_seconds"] > 0.
    for result in results:
        assert result["repeated"]["cache"] == "memory"
        assert result["repeated"]["model_seconds"] == result["repeated"]["support_seconds"] == 0.
