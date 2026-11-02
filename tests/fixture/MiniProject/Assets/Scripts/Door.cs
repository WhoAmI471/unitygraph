using UnityEngine;

namespace Mini
{
    public enum DoorState { Closed, Open, Locked }

    /// <summary>A door that opens when the player is near. Second sentence is dropped.</summary>
    public class Door : MonoBehaviour
    {
        private const string KeyPath = "Assets/Prefabs/Key.prefab";

        [SerializeField] private DoorState state;
        public float speed = 2f;
        [SerializeField] private Transform hinge;
        [SerializeField] private GameObject keyPrefab;
        [SerializeField] private LayerMask blockers;
        private bool busy; // runtime only

        public bool IsOpen => state == DoorState.Open;

        public void Open(float delay) { busy = true; }

        private void Update() { }

        private Material Glow() => new Material(Shader.Find("Mini/Glow"));
    }
}
