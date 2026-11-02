"""End-to-end tests: run the CLI against a tiny hand-written Unity project (tests/fixture/MiniProject)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, '..', 'src', 'unitygraph', 'core.py')
MAIN = os.path.join(HERE, '..', 'src', 'unitygraph', '__main__.py')
FIXTURE = os.path.join(HERE, 'fixture', 'MiniProject')


class MiniProjectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.root = os.path.join(cls.tmp, 'MiniProject')
        shutil.copytree(FIXTURE, cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ug(self, *args, cwd=None):
        res = subprocess.run([sys.executable, CORE, *args], cwd=cwd or os.path.join(self.root, 'Assets'),
                             capture_output=True, text=True, encoding='utf-8', stdin=subprocess.DEVNULL)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_map(self):
        out = self.ug('map')
        self.assertIn('(Unity 6000.0.30f1)', out)
        self.assertIn('- Door : MonoBehaviour — A door that opens when the player is near.', out)
        self.assertNotIn('Second sentence', out)
        self.assertIn('- enums: DoorState', out)
        self.assertIn('- Main (6 objects): Level, Front Door, Sun', out)
        self.assertIn('- Prefabs/DoorPrefab.prefab: Door', out)
        self.assertIn('- params IsOpen; states Closed, Opening: Door', out)

    def test_scene_hierarchy_groups_instances_and_resolves_overrides(self):
        out = self.ug('scene', 'Main')
        self.assertIn('2 GameObjects, 4 prefab instances', out)
        self.assertIn('Level [Spawner(target=@Front Door.Door, count=5)]', out)
        self.assertIn('  Key* ×3 ⇐Key.prefab', out)
        self.assertIn('Front Door ⇐DoorPrefab.prefab {Door.state=Open}', out)  # enum by name, trivial mods hidden
        self.assertIn('Sun [Light]', out)

    def test_scene_path_and_depth(self):
        self.assertIn('Level [Spawner', self.ug('scene', 'Main', '--path', 'Level'))
        self.assertIn('…+3', self.ug('scene', 'Main', '--depth', '1'))
        self.assertIn('no object at path', self.ug('scene', 'Main', '--path', 'Nope'))

    def test_prefab_fields_refs_and_layer_mask(self):
        out = self.ug('prefab', 'DoorPrefab')
        self.assertIn('Door [Door(state=Locked, speed=3, hinge=@Hinge, keyPrefab=→Key.prefab, '
                      'blockers=Default|Interactable, loot=→Key.prefab (addr "keys/gold"))] › Hinge', out)

    def test_class_report(self):
        out = self.ug('class', 'Door')
        self.assertIn('class Mini.Door : MonoBehaviour   (Assets/Scripts/Door.cs:8)', out)
        self.assertIn('serialized: state: DoorState; speed: float; hinge: Transform; keyPrefab: GameObject; '
                      'blockers: LayerMask', out)
        self.assertIn('fields: KeyPath: string static; busy: bool', out)
        self.assertIn('methods: Open(float)', out)
        self.assertIn('unity: Update', out)
        self.assertIn('used by: Spawner', out)
        self.assertIn('assets in code: Key.prefab, shader "Mini/Glow" (Glow.shader)', out)
        self.assertIn('attached: DoorPrefab.prefab: Door', out)

    def test_partial_class_is_merged(self):
        out = self.ug('class', 'Spawner')
        self.assertIn('Spawner.cs:6 + Spawner.Extra.cs', out)
        self.assertIn('serialized: target: Door; count: int', out)
        self.assertIn('methods: Spawn(); Clear()', out)

    def test_enum(self):
        self.assertIn('values: Closed=0, Open=1, Locked=2', self.ug('class', 'DoorState'))

    def test_where(self):
        out = self.ug('where', 'Door')
        self.assertIn('DoorPrefab.prefab: Door', out)
        self.assertIn('via prefab in Main.unity: DoorPrefab', out)
        self.assertIn('Main.unity: Sun', self.ug('where', 'Light'))

    def test_refs(self):
        out = self.ug('refs', 'Key.prefab')
        self.assertIn('Assets/Scenes/Main.unity — Level/Key×3', out)
        self.assertIn('Assets/Prefabs/DoorPrefab.prefab', out)
        self.assertIn('named in code by: Door', out)
        self.assertIn('named in code by: Door ("Mini/Glow")', self.ug('refs', 'Glow.shader'))

    def test_find(self):
        out = self.ug('find', '^Key')
        self.assertIn('Main.unity: Level/Key ×3 ⇐Key.prefab', out)
        self.assertIn('nothing named like', self.ug('find', 'zzz'))

    def test_anim(self):
        out = self.ug('anim', 'Door')
        self.assertIn('params: IsOpen:bool', out)
        self.assertIn('*Closed = (none)  → Opening if IsOpen', out)
        self.assertIn('Opening = (none) speed 2', out)

    def test_graph_and_build_outputs(self):
        self.assertIn('8 nodes', self.ug('graph'))
        with open(os.path.join(self.root, '.unitygraph', 'graph.html'), encoding='utf-8') as f:
            html = f.read()
        self.assertIn('"label": "Door"', html)
        self.ug('build')
        for rel in ('MAP.md', 'scenes/Main.md', 'prefabs/Prefabs_DoorPrefab.md', 'code/Mini.md', 'anim/Animations_Door.md'):
            self.assertTrue(os.path.exists(os.path.join(self.root, '.unitygraph', rel)), rel)

    def test_calls(self):
        out = self.ug('calls', 'Door.Open')
        self.assertIn('Mini.Door.Open  (Door.cs:', out)
        self.assertIn('called by: Spawner.Spawn (Spawner.cs:9)', out)
        self.assertIn('Spawn: -', self.ug('calls', 'Spawner'))
        self.assertIn('no type "Nope"', self.ug('calls', 'Nope.Open'))

    def test_query(self):
        out = self.ug('query', 'Door.speed>2')
        self.assertIn('2 of 2 Door components match', out)
        self.assertIn('Main.unity: Front Door  speed=3', out)  # instance inherits the prefab value
        out = self.ug('query', 'Door.state')
        self.assertIn('Locked ×1  e.g. DoorPrefab.prefab:Door', out)
        self.assertIn('Open ×1  e.g. Main.unity:Front Door', out)  # instance override wins
        self.assertIn('no Door matches', self.ug('query', 'Door.state=Closed'))
        self.assertIn('has no serialized field nope', self.ug('query', 'Door.nope=1'))

    def test_formats(self):
        out = self.ug('input')
        self.assertIn('Move (Value Vector2): Keyboard:WASD(up=w, down=s), Gamepad/leftStick', out)
        self.assertIn('Use (Button) [Hold]: Keyboard/e', out)
        self.assertIn('actions named in code: Use: Spawner.Extra', out)
        out = self.ug('ui', 'Menu.uxml')
        self.assertIn('VisualElement#root.panel.dark', out)
        self.assertIn('  Button#play.big text="Play"', out)
        self.assertIn('.big:hover, #play  {font-size}', self.ug('ui', 'Menu.uss'))
        out = self.ug('material', 'Glow')
        self.assertIn('shader: "Mini/Glow"', out)
        self.assertIn('_Color = #FF8000', out)
        self.assertIn('render queue: 3000', out)
        self.assertIn('"keys/gold" → Assets/Prefabs/Key.prefab  [loot]', self.ug('addr'))
        out = self.ug('refs', 'Key.prefab')
        self.assertIn('addressable: "keys/gold" in group Default Local Group, labels loot', out)
        self.assertIn('Spawner ("keys/gold")', out)
        out = self.ug('map')
        self.assertIn('- Input/Controls.inputactions: Player: Move, Use', out)
        self.assertIn('- group Default Local Group: 1 entries', out)

    def test_mcp_server(self):
        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'unitygraph', 'arguments': {'args': 'query "Door.state=Open"', 'root': self.root}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'unitygraph', 'arguments': {'args': 'scene', 'root': self.root}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'nope'},
        ]
        res = subprocess.run([sys.executable, MAIN, 'mcp'], input='\n'.join(json.dumps(m) for m in msgs) + '\n',
                             capture_output=True, text=True, encoding='utf-8')
        replies = {r['id']: r for r in map(json.loads, res.stdout.splitlines())}
        self.assertEqual(sorted(replies), [1, 2, 3, 4, 5])  # no reply to the notification
        self.assertEqual(replies[1]['result']['serverInfo']['name'], 'unitygraph')
        self.assertEqual([t['name'] for t in replies[2]['result']['tools']], ['unitygraph'])
        self.assertIn('Main.unity: Front Door  state=Open', replies[3]['result']['content'][0]['text'])
        self.assertFalse(replies[3]['result']['isError'])
        self.assertTrue(replies[4]['result']['isError'])
        self.assertEqual(replies[5]['error']['code'], -32601)

    def test_diff_against_git(self):
        repo = os.path.join(self.tmp, 'diffrepo')
        shutil.copytree(self.root, repo, ignore=shutil.ignore_patterns('.unitygraph'))
        git = ['git', '-C', repo, '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        for args in (['init', '-q'], ['add', '-A'], ['commit', '-q', '-m', 'base']):
            subprocess.run(git + args, check=True, capture_output=True)
        scene = os.path.join(repo, 'Assets', 'Scenes', 'Main.unity')
        with open(scene, encoding='utf-8') as f:
            text = f.read()
        text = text.replace('m_Name: Sun', 'm_Name: Moon')
        text = text.replace('propertyPath: state\n      value: 1', 'propertyPath: state\n      value: 2')
        with open(scene, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        out = self.ug('diff', cwd=repo)
        self.assertIn('## Assets/Scenes/Main.unity  (HEAD → working tree)', out)
        self.assertIn('~ Moon: renamed from "Sun"', out)
        self.assertIn('~ Front Door: override +Door.state=Locked; override -Door.state=Open', out)
        self.assertIn('no scene or prefab changed since HEAD~0', self.ug('diff', '--rev', 'HEAD~0', '--to', 'HEAD', cwd=repo))

    def test_unknown_names(self):
        self.assertIn('no type "Nope"', self.ug('class', 'Nope'))
        self.assertIn('no scene matches', self.ug('scene', 'Nope'))

    def test_incremental_refresh_sees_edits(self):
        path = os.path.join(self.root, 'Assets', 'Prefabs', 'Key.prefab')
        self.ug('prefab', 'Key')
        with open(path, encoding='utf-8') as f:
            text = f.read()
        time.sleep(0.05)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text.replace('m_Name: Key', 'm_Name: Golden Key'))
        try:
            self.assertIn('Golden Key [BoxCollider]', self.ug('prefab', 'Key'))
        finally:
            with open(path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)


class ValueRenderingTest(unittest.TestCase):
    """Unity serializes float.PositiveInfinity as "Infinity"; rendering must not crash on it."""

    def setUp(self):
        sys.path.insert(0, os.path.join(HERE, '..', 'src'))
        from unitygraph import core
        self.core = core

    def test_numbers(self):
        f = self.core._fmt_num
        self.assertEqual(f('3'), '3')
        self.assertEqual(f('0.35'), '0.35')
        self.assertEqual(f('Infinity'), 'inf')
        self.assertEqual(f('-Infinity'), '-inf')
        self.assertEqual(f('NaN'), 'nan')
        self.assertEqual(f('1e20'), '1e+20')

    def test_colors_with_infinity(self):
        ctx = self.core.Ctx(None, {'gos': {}, 'trs': {}, 'st': {}, 'comps': {}, 'pis': {}}, {})
        self.assertEqual(ctx.value({'r': '1', 'g': '0.5', 'b': '0', 'a': '2'}), '#FF8000FF')  # HDR alpha clamps
        self.assertIn('inf', ctx.value({'r': 'Infinity', 'g': '0', 'b': '0', 'a': '1'}))


class GuardTest(unittest.TestCase):
    """The PreToolUse hook: whole-file reads of big Unity YAML are denied, targeted ones pass."""

    def setUp(self):
        sys.path.insert(0, os.path.join(HERE, '..', 'src'))
        from unitygraph import guard
        self.guard = guard
        self.scene = os.path.join(FIXTURE, 'Assets', 'Scenes', 'Main.unity')

    def check(self, tool, **inp):
        return self.guard.check({'tool_name': tool, 'tool_input': inp, 'cwd': FIXTURE}, threshold_kb=1)

    def test_denies_whole_reads(self):
        reason = self.check('Read', file_path=self.scene)
        self.assertIn('Main.unity is', reason)
        self.assertIn('unitygraph scene "Main" --depth 2', reason)
        self.assertIn('unitygraph prefab "DoorPrefab"', self.check('Bash', command='cat Assets/Prefabs/DoorPrefab.prefab'))

    def test_allows_targeted_reads(self):
        self.assertIsNone(self.check('Read', file_path=self.scene, offset=10, limit=20))
        self.assertIsNone(self.check('Bash', command='grep -n Door Assets/Scenes/Main.unity'))
        self.assertIsNone(self.check('Bash', command='Get-Content Assets/Scenes/Main.unity -TotalCount 20'))
        self.assertIsNone(self.check('Read', file_path=os.path.join(FIXTURE, 'Assets', 'Scripts', 'Door.cs')))
        self.assertIsNone(self.guard.check({'tool_name': 'Read', 'tool_input': {'file_path': self.scene},
                                            'cwd': FIXTURE}, threshold_kb=64))  # small file

    def test_hook_output(self):
        event = json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': self.scene}, 'cwd': FIXTURE})
        res = subprocess.run([sys.executable, MAIN, 'guard'], input=event, capture_output=True, text=True,
                             env=dict(os.environ, UNITYGRAPH_GUARD_KB='1'))
        out = json.loads(res.stdout)['hookSpecificOutput']
        self.assertEqual(out['permissionDecision'], 'deny')
        res = subprocess.run([sys.executable, MAIN, 'guard'], input=event, capture_output=True, text=True,
                             env=dict(os.environ, UNITYGRAPH_GUARD_KB='1', UNITYGRAPH_GUARD='off'))
        self.assertEqual(res.stdout, '')


if __name__ == '__main__':
    unittest.main()
