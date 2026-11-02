namespace Mini
{
    public partial class Spawner
    {
        public int count = 3;
        public void Clear() { }

        public void LoadGold()
        {
            var handle = UnityEngine.AddressableAssets.Addressables.LoadAssetAsync<UnityEngine.GameObject>("keys/gold");
            var use = actions.FindAction("Use");
        }
    }
}
