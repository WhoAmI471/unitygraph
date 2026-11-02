---
name: unitygraph
description: Navigate a Unity project (a folder with Assets/ and ProjectSettings/) through compact summaries instead of raw YAML. Use before reading any .unity, .prefab, .controller or .asset file, and whenever you need to know what is in a scene or prefab, which components and serialized values an object has, where a script, component or asset is used, what a C# type looks like and who uses it, or how an animator controller is wired.
---

# unitygraph

Unity scenes and prefabs are YAML full of fileIDs and GUIDs; a single scene can be tens of megabytes.
`unitygraph` reads them (plus `.meta`, C# and animator files) straight from disk, resolves every
reference to a name, and prints summaries that cost a tiny fraction of the tokens. It needs only
Python 3.9+ and no running editor. Each call refreshes its index incrementally (cache in
`.unitygraph/` at the project root), so answers match the files on disk.

**Do not open `.unity` / `.prefab` / `.controller` files to understand them. Query them.**
Read raw YAML only to edit a specific line the summary already pointed you to. The plugin's hook enforces
this: a whole-file Read (or `cat`) of Unity YAML over 32 KB is denied with the command to run instead;
Grep, `head`, `sed -n` and Read with `offset`/`limit` still work for targeted edits.

## Running it

The plugin puts `unitygraph` on the Bash PATH. Run it from anywhere inside the Unity project:

```bash
unitygraph map
```

If the bare command is not found (for example in PowerShell), run the bundled script directly:
`python "${CLAUDE_PLUGIN_ROOT}/src/unitygraph/core.py" map`

## Commands

| Command | Answers |
|---|---|
| `unitygraph map` | Start here. Assemblies, every project type with a one-line summary, scenes with their roots, prefabs with their scripts, data assets, animator controllers |
| `unitygraph scene <name> [--depth N] [--path "A/B"] [--full]` | Hierarchy with components, serialized field values and prefab overrides. Use `--depth 2` first on big scenes, then `--path` into the part you need |
| `unitygraph prefab <name>` | Same for a prefab (variants show their base) |
| `unitygraph class <Name>` | Fields (serialized vs runtime), public API, network/attribute methods, Unity messages, uses / used by, derived types, asset paths and shaders named in its code, where it is attached |
| `unitygraph calls <Class.Method\|Method\|Class>` | Call graph: what a method calls and who calls it (typed through fields, parameters, locals, `GetComponent<T>()`), property readers, `nameof`/`+=` references such as SyncVar hooks; for a class, which other classes use each member |
| `unitygraph where <Script\|Component\|asset>` | Every object a script or built-in component (`Light`, `Animator`) sits on, including via prefabs; for an asset, who references it |
| `unitygraph query "<Class.field OP value>[, field OP value]"` | Objects whose component values match (`= != > < >= <= ~regex`), prefab instances with their effective value; `"Class.field"` alone shows the distribution of values |
| `unitygraph refs <asset>` | What an asset references and what references it, including code that names it by path, Addressables address and labels |
| `unitygraph find "<regex>"` | GameObjects by name across project scenes and prefabs, with paths |
| `unitygraph diff [scene\|prefab ...] [--rev REV] [--to REV]` | Semantic diff against git (default: everything changed since HEAD): objects added/removed/renamed/moved/toggled, components and values changed, overrides changed. Use it to review or describe scene changes instead of `git diff` |
| `unitygraph anim <name>` | Animator controller: parameters, layers, states, blend trees, transitions with conditions |
| `unitygraph input [name]` | Input System actions: maps, actions, bindings and composites, which code names each action |
| `unitygraph ui <name>` | UI Toolkit: UXML element tree (`Type#name.class`) or USS selectors |
| `unitygraph material <name>` | Shader, keywords, textures, colors, and the floats that differ from other materials on the same shader |
| `unitygraph addr [regex]` | Addressables: address → asset, groups, labels |
| `unitygraph asset <name>` | Fields of a ScriptableObject asset |
| `unitygraph graph` | Writes `.unitygraph/graph.html`, an interactive graph for the user to open in a browser |
| `unitygraph build` | Rewrites the pre-rendered `.unitygraph/*.md` copies (queries do this automatically after changes) |

## Reading the output

```
Ship [NavMeshLink, SailRig(sails=[@Sail 0, @Sail 1], seeThrough=[→Sail.mat])]
  Model › Ship_Large ⇐Ship_Large.fbx [+MeshCollider]
  Bucket ×3 [NetworkIdentity, BoxCollider, Carryable(itemType=Bucket)]  …+3
  Nature  (924 decor: CommonTree_3×23, PineTree_2×24, … +25 kinds)
```

- `Name ⇐Source.prefab` is a prefab instance; `{prop=value}` are its non-trivial overrides; `[+Comp]` are components added on the instance; `inner:` marks an override on an unnamed node inside a model file.
- `[Comp(field=value)]` shows serialized fields that differ from empty/zero; enums print by name. `@Object` is a reference to an object in the same file, `→Asset.ext` a reference to an asset (`→Model.fbx:Clip` for a clip inside a model).
- `A › B` means B is A's only child. `×N` merges identical siblings (including identical subtrees). `…+N` means N more objects below the depth limit. `(N decor: …)` collapses children that have no scripts or overrides; pass `--path` to list them.
- `(off)` is an inactive object or disabled component, `#Tag` a non-default tag.
- Default, empty and per-instance ids (Mirror `sceneId`) are hidden; add `--full` to see everything.

## Good habits

- Begin a Unity task with `unitygraph map`, then narrow down with `scene --depth 2`, `--path`, `class`, `where`.
- Before changing a method, run `unitygraph calls Class.Method` to see every caller; to find objects by their settings, use `query` instead of scanning scenes.
- After editing scenes or prefabs (or when asked what changed), summarize with `unitygraph diff`.
- After changing scenes or prefabs in the editor, they must be saved before unitygraph sees the change.
- If scenes are generated by editor code, `refs <scene>` shows the code that names them (`named in code by: …`); change the generator, not the scene.
- Suggest adding `/.unitygraph/` to the project's `.gitignore` if it is not there.
- Third-party folders (`Assets/Packages/`, `Assets/Plugins/`, `Assets/ThirdParty/`, `Assets/Samples/`, …) are indexed for name resolution but left out of the map. A project can list more in `.unitygraph.json`: `{"third_party": ["Assets/SomeVendor/"]}`.
