"""CPU fallback for models that hardcode CUDA (upstream Curate v2.8.0).

- TransNetV2: `.cuda()` in load, `device="cuda"` default in predict.
- Cosmos-Embed1: `.to("cuda", dtype=torch.bfloat16)` for model, video and text tensors; on CPU, bf16 only with AMX, else fp32.
- Cosmos-Embed1 stage: with 0 GPUs it asks for 1 CPU, so Xenna packs ~14 model copies per 16-vCPU node
  and the node OOMs. Without a GPU, request EMBED_CPUS_PER_WORKER CPUs (default 4) per worker instead.
Usage: patch_cpu.py <code_dir>
"""

import pathlib
import sys

DEV = '"cuda" if torch.cuda.is_available() else "cpu"'
PATCHES = {
    "cosmos_curator/models/transnetv2.py": [
        ("return model.eval().cuda()", f"return model.eval().to({DEV})", 1),
        ('    device: str = "cuda",\n) -> npt.NDArray[np.bool_]:', f"    device: str = {DEV},\n) -> npt.NDArray[np.bool_]:", 1),
    ],
    "cosmos_curator/models/cosmos_embed1.py": [
        ("import torch\n",
         "import torch\n\n"
         "# bf16 is only fast on CPUs with AMX (c7i/m7i); measured 5x slower than fp32 on c5\n"
         "# and 125x slower on c6a/m6a, which made one embedding run take over an hour.\n"
         "EMBED_DTYPE = (torch.bfloat16 if torch.cuda.is_available() or 'amx_bf16' in open('/proc/cpuinfo').read()\n"
         "               else torch.float32)\n", 1),
        ('.to("cuda", dtype=torch.bfloat16)', f".to({DEV}, dtype=EMBED_DTYPE)", 3),
    ],
    "cosmos_curator/pipelines/video/embedding/cosmos_embed1_stages.py": [
        ("        return CuratorStageResource(gpus=self._num_gpus_per_worker)\n",
         "        if not self._num_gpus_per_worker:\n"
         "            import os\n\n"
         '            return CuratorStageResource(cpus=float(os.environ.get("EMBED_CPUS_PER_WORKER", "4")))\n'
         "        return CuratorStageResource(gpus=self._num_gpus_per_worker)\n", 1),
    ],
}

root = pathlib.Path(sys.argv[1])
for rel, subs in PATCHES.items():
    path = root / rel
    src = path.read_text()
    for old, new, want in subs:
        got = src.count(old)
        if got != want:
            sys.exit(f"{rel}: expected {want} x {old!r}, found {got}")
        src = src.replace(old, new)
    path.write_text(src)
    print(f"patched {rel}")
