"""Information cost benchmark: how much text an agent has to read to answer typical questions about a
Unity project, with plain tools (grep / reading files / git diff) versus unitygraph.

Each task pairs the commands a careful agent would run without the index (the cheapest sensible way,
not a strawman) with the unitygraph command that answers the same question. We measure the bytes of
output both produce; tokens are estimated as bytes / 4. Run from the Unity project root:

    python path/to/benchmarks/measure.py tasks_storm_crew.json
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UG = [sys.executable, os.path.join(HERE, '..', 'src', 'unitygraph', '__main__.py')]


def run(cmd):
    if isinstance(cmd, str):
        res = subprocess.run(['bash', '-c', cmd], capture_output=True, stdin=subprocess.DEVNULL)
    else:
        res = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL)
    return res.stdout


def main(tasks_file):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    with open(tasks_file, encoding='utf-8') as f:
        tasks = json.load(f)
    run(UG + ['build'])  # warm the cache so timings are about queries, not the first index
    rows = []
    for t in tasks:
        base = sum(len(run(c)) for c in t['baseline'])
        ug = sum(len(run(UG + c)) for c in t['unitygraph'])
        rows.append((t['task'], base, ug, t.get('note', '')))
    print('| Question | Without unitygraph | With unitygraph | Ratio | Note |')
    print('|---|---:|---:|---:|---|')
    tb = tu = 0
    for task, base, ug, note in rows:
        tb, tu = tb + base, tu + ug
        print(f'| {task} | {base / 4 / 1000:,.1f}k tok | {ug / 4 / 1000:,.1f}k tok | {base / max(ug, 1):,.0f}× | {note} |')
    print(f'| **Total** | **{tb / 4 / 1000:,.0f}k tok** | **{tu / 4 / 1000:,.1f}k tok** | **{tb / max(tu, 1):,.0f}×** | |')


if __name__ == '__main__':
    main(sys.argv[1])
