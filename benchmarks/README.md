# Benchmark: what an agent has to read

`measure.py` compares, for typical questions about a Unity project, how much text an agent reads to get
the answer with plain tools versus with unitygraph. The baseline for each question is the cheapest
sensible command set (grep by script GUID, reading the one file that holds the answer, `git diff`),
not a strawman. Output size is measured in bytes; tokens are estimated as bytes / 4.

It measures input size only. It does not count the extra turns the baseline needs (the agent still has to
map fileIDs to objects, enum numbers to names, clip fileIDs to clip names), nor answer quality.

## storm-crew (Unity 6, Mirror; 98 scripts, 3 generated 15 MB scenes)

Measured 2026-11-02 with unitygraph 0.2.0 ([tasks](tasks_storm_crew.json)):

| Question | Without unitygraph | With unitygraph | Ratio | Note |
|---|---:|---:|---:|---|
| Project overview: systems and what each class does | 29.1k tok | 4.1k tok | 7× | baseline: first 30 lines of each of 98 scripts |
| What is on the ship in Voyage_Brig (hierarchy, 2 levels) | 113.0k tok | 1.1k tok | 102× | baseline gets names and parent links only, still without components |
| Where is the Leak script used, with which settings | 3.4k tok | 0.1k tok | 33× | baseline field values reference objects by fileID |
| Which item types lie on each ship | 2.6k tok | 0.2k tok | 15× | baseline gets `itemType: 5` and has to map numbers to the enum |
| Who reads Carryable.IsHeld | 0.2k tok | 0.1k tok | 3× | grep is already cheap here; unitygraph adds the enclosing methods |
| Player prefab: components and settings | 8.2k tok | 0.5k tok | 15× | |
| How the Anne Locomotion animator is wired | 2.5k tok | 0.1k tok | 17× | baseline clips are fileIDs inside the FBX |
| Input bindings | 10.3k tok | 0.5k tok | 20× | |
| What changed in Voyage_Brig in commit 5d3c7fe | 5,776.6k tok | 0.8k tok | 7,576× | a generated scene: the builder rewrites every fileID |

**Reading it honestly:**

- The scene diff dominates any total: 5.8M tokens is more than a context window, so without unitygraph an
  agent cannot answer that question at all, it can only sample. Leaving it out, the other eight questions
  cost **~169k tokens without vs ~6.7k with (≈25× less)**; the median per question is **16×**.
- Where plain text search already fits (a member used in C#), the gain is small (3×).
- The biggest wins are scene and prefab questions, where the raw files are fileID soup.

Reproduce from the Unity project root:

```bash
python path/to/unitygraph/benchmarks/measure.py path/to/unitygraph/benchmarks/tasks_storm_crew.json
```

Copy `tasks_storm_crew.json` and adjust the GUIDs, names and commits to benchmark another project.
