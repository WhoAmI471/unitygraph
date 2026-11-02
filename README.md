# unitygraph

**A compact, queryable index of a Unity project, made for AI coding agents.**

Unity scenes and prefabs are YAML full of `fileID`s and GUIDs. One generated scene can easily be
15 MB, which is millions of tokens of noise for an agent like Claude Code. `unitygraph` reads
scenes, prefabs, `.meta` files, C# sources and animator controllers straight from disk, resolves
every reference to a name, and prints short summaries the agent can actually use.

| | Raw files | unitygraph |
|---|---|---|
| A 15.5 MB generated scene (3.7k GameObjects, 3k prefab instances) | ~4M tokens | 22 KB full hierarchy, 8 KB at `--depth 2` |
| "Where is `Leak` used?" | grep through megabytes of YAML by GUID | one 4-line answer |

- **No editor required.** Plain Python 3.9+, standard library only. Works on Windows, macOS, Linux.
- **Always fresh.** Every query refreshes an incremental cache (`.unitygraph/` in the project root), so answers match the files on disk.
- **Built for reading.** Names instead of IDs: enum values by name, `@Object` scene references, `→Asset` asset references, clip names inside FBX files, layer names for masks.

## Example

```
$ unitygraph scene Voyage_Brig --path Ship --depth 2
Ship [NavMeshLink, SailRig(sails=[@Sail 0, @Sail 1, @Sail 2, @Sail 3], foreAft=0, …)]
  Model › Ship_Large ⇐Ship_Large.fbx [+MeshCollider]
  Leak* ×6 [NetworkIdentity, BoxCollider, Leak(nailsRequired=4, boardedFlow=0.35, …)]
  Hatch [NetworkIdentity, BoxCollider, Hatch(destination=@Destination, goesDown=true)] › Destination
  Bucket ×3 [NetworkIdentity, BoxCollider, Carryable(itemType=Bucket), Bucket(scoopAmount=0.06)]  …+3
  Rope Ladder Port  (31 decor: Rope×20, Rung×11)

$ unitygraph class Carryable
class StormCrew.Items.Carryable : Interactable   (Assets/Scripts/Items/Carryable.cs:25)
  An item a player can pick up.
  serialized: itemType: ItemType
  fields: holder: GameObject [SyncVar]; restPosition: Vector3 [SyncVar]; …
  net/attr: [Server] Interact(PlayerInteractor); [Server] ServerSetHolder(PlayerInteractor); …
  used by: Bucket, CannonballPile, CargoCrate, PlayerInteractor, VoyageHud, WorldMarkers
  attached: Voyage_Brig.unity: Ship/Bucket×3, Ship/Hammer×3, Ship/Hook; Plank.prefab: Plank

$ unitygraph anim "Anne Locomotion"
params: Speed:float, Airborne:bool, Crouch:bool, Work:bool
layer Base Layer
  Any → Air if Airborne
  *Locomotion = blend Speed [Characters_Anne.fbx:Idle@0, Characters_Anne.fbx:Walk@0.45, Characters_Anne.fbx:Run@1]
  Air = Characters_Anne.fbx:Jump_Idle  → Locomotion if !Airborne
```

## Install

### As a Claude Code plugin (recommended)

```
/plugin marketplace add WhoAmI471/unitygraph
/plugin install unitygraph@whoami471
```

The plugin adds a skill that tells Claude to query the index instead of opening scene YAML,
and puts the `unitygraph` command on Claude's Bash `PATH`. Nothing to configure per project.

### As a command-line tool

```bash
pipx install git+https://github.com/WhoAmI471/unitygraph
```

or `pip install git+https://github.com/WhoAmI471/unitygraph`. Without installing anything you can also
run `python path/to/unitygraph/src/unitygraph/core.py <command>`.

## Commands

Run them anywhere inside a Unity project (a folder with `Assets/` and `ProjectSettings/`).

| Command | What it shows |
|---|---|
| `unitygraph map` | Project overview: assemblies, every project type with a one-line summary, scenes, prefabs, data assets, animator controllers |
| `unitygraph scene <name> [--depth N] [--path "A/B"] [--full]` | GameObject hierarchy with components, serialized values and prefab overrides |
| `unitygraph prefab <name>` | Same for a prefab (variants show their base) |
| `unitygraph class <Name>` | Serialized and runtime fields, public API, RPC/attribute methods, Unity messages, uses / used by, derived types, asset paths and shaders named in code, where it is attached |
| `unitygraph where <Script\|Component\|asset>` | Every object a script or built-in component sits on (directly or via prefabs), or who references an asset |
| `unitygraph refs <asset>` | What an asset references and what references it, including code that names it by path |
| `unitygraph find "<regex>"` | GameObjects by name across project scenes and prefabs |
| `unitygraph anim <name>` | Animator controller: parameters, layers, states, blend trees, transitions with conditions |
| `unitygraph asset <name>` | Fields of a ScriptableObject asset |
| `unitygraph graph` | Writes `.unitygraph/graph.html`: an interactive graph of types, scenes, prefabs and animators with their links |
| `unitygraph build` | Writes pre-rendered copies of everything to `.unitygraph/*.md` |

### Reading the output

- `Name ⇐Source.prefab {prop=value}` is a prefab instance with its non-trivial overrides; `[+Comp]` are components added on the instance.
- `[Comp(field=value)]` lists serialized fields that are not empty or zero. `@Object` is an object in the same file, `→Asset.ext` an asset, `→Model.fbx:Clip` a clip inside a model.
- `A › B`: B is A's only child. `×N`: identical siblings (or identical subtrees) merged. `…+N`: N more objects below the depth limit. `(N decor: …)`: children without scripts or overrides, collapsed; use `--path` to list them.
- `(off)` is an inactive object or disabled component, `#Tag` a non-default tag. `--full` shows defaults and everything that was hidden.

### Configuration

Third-party folders (`Assets/Packages/`, `Assets/Plugins/`, `Assets/ThirdParty/`, `Assets/Samples/`,
`Assets/TextMesh Pro/`) are indexed for name resolution but kept out of the map. Add your own in
`.unitygraph.json` at the project root:

```json
{ "third_party": ["Assets/SomeVendor/", "Assets/Imported/"] }
```

Add `/.unitygraph/` to your `.gitignore`.

## Limitations

- Reads files on disk: save scenes in the editor before querying.
- Text serialization only (Unity's default "Force Text" asset serialization).
- C# is parsed with regular expressions, not a compiler: good for declarations, fields and references, not for every corner of the language.
- Overrides on nodes *inside* model files (FBX) show as `inner:` because Unity 6 does not store their names in `.meta` files.
- Addressables are not indexed yet.

## Development

```bash
python -m unittest discover -s tests -v
```

The tests run every command against `tests/fixture/MiniProject`, a tiny hand-written Unity project.

## Русский

`unitygraph` строит компактный индекс Unity-проекта, чтобы ИИ-агенты (Claude Code и другие) читали
короткие сводки вместо мегабайтных YAML-сцен. Установка как плагин Claude Code:
`/plugin marketplace add WhoAmI471/unitygraph`, затем `/plugin install unitygraph@whoami471`.
Начинать с `unitygraph map`, дальше `scene`, `class`, `where`, `refs`.

## License

MIT
