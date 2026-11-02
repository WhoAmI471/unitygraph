"""End-to-end tests: run the CLI against a tiny hand-written Unity project (tests/fixture/MiniProject)."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(HERE, '..', 'src', 'unitygraph', 'core.py')
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
                      'blockers=Default|Interactable)] › Hinge', out)

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
        self.assertIn('7 nodes', self.ug('graph'))
        with open(os.path.join(self.root, '.unitygraph', 'graph.html'), encoding='utf-8') as f:
            html = f.read()
        self.assertIn('"label": "Door"', html)
        self.ug('build')
        for rel in ('MAP.md', 'scenes/Main.md', 'prefabs/Prefabs_DoorPrefab.md', 'code/Mini.md', 'anim/Animations_Door.md'):
            self.assertTrue(os.path.exists(os.path.join(self.root, '.unitygraph', rel)), rel)

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


if __name__ == '__main__':
    unittest.main()
