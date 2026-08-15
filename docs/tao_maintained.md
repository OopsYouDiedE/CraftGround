# `tao-maintained` branch

This branch starts from upstream commit
`18eba01a87a8481bc4fbb54b42fee1428c0c3719` and carries the runtime behavior
required by `tao-not-42`.

## Maintained changes

- Minecraft 1.21 provides `memorysnapshot save` and `memorysnapshot load`
  commands for in-process rollout checkpoints.
- POSIX observation shared memory grows to the serialized observation size
  before the Java runtime maps and writes a frame.
- BoostIPC reserves a 1 MiB variable-length action buffer instead of deriving
  capacity from a protobuf no-op, which serializes to zero bytes.
- Environment restart reuses the initial IPC allocation and recreates IPC only
  after it has been destroyed.
- The mc121 runtime package vendors the native capture sources that its CMake
  project compiles. This keeps wheels and Git subdirectory installations
  independent of the original repository checkout.
- TaoNot42-specific environment modules live at the repository root. When this
  repository is checked out at `TaoNot42/envs/craftground`, they form the
  `envs.craftground` package and use `minecraft/mc121` from the same checkout.

The fork packages use PEP 440 local versions `2.7.8+tao.2` and
`0.1.0+tao.2`. TaoNot42 pins the exact Git commit through its CraftGround
submodule; these version labels identify the fork but do not replace source
locking.

## Dependency constraints

| Layer | Declared constraint |
| --- | --- |
| Python | `>=3.9` on runtime packages; the core package does not declare `requires-python` |
| Core runtime | `gymnasium`, `Pillow`, `numpy`, `typing_extensions`, `psutil`, `torch`, and `craftground-runtime-mc121` have no version bounds |
| Protobuf | `protobuf>=5.0.0`, with no upper bound |
| Optional JAX | `jax[cuda]`, or `jax` plus `jax-metal`, with no version bounds |
| Python build | `setuptools>=42`, `cmake>=3.12`; `scikit-build-core`, `pybind11`, `wheel`, and `ninja` are unbounded |
| Runtime-package build | `setuptools>=61` |
| Runtime native build | CMake `>=3.28`, JDK 21, JNI, and OpenGL; GLEW is required outside macOS |
| Optional native features | PNG `>=1.6` and CUDA Toolkit are detected when available |
| mc121 | Minecraft `1.21`, Yarn `1.21+build.9`, Fabric Loader `0.15.11`, Fabric API `0.100.6+1.21`, Gradle `8.8`, Kotlin `2.0.0` |
| mc262 | Minecraft `26.2`, Fabric Loader `0.19.3`, Fabric API `0.155.2+26.2`, Loom `1.17-SNAPSHOT`, Kotlin `2.0.0` |
| Vendored/fetched native libraries | GLM is pinned to commit `2d4c4b4dd31fde06cfffad7915c2b3006402322f`; Boost is declared at `boost-1.87.0` |

These are upstream declarations, not a compatibility guarantee. In
particular, most Python dependencies remain resolver-dependent unless the
consumer project locks them.

## Upstream release rule

Keep a change in this branch until an upstream release exposes equivalent
behavior through a documented public API and the `tao-not-42` real
CraftGround validation passes against that release. The presence of similar
internal code or an unverified upstream commit is not sufficient to remove a
maintained change.
