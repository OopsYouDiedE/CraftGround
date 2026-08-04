from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_mc121_runtime_package_contains_native_capture_sources() -> None:
    runtime_cpp = REPOSITORY_ROOT / "minecraft/mc121/src/main/cpp"
    required_files = (
        "noboost_ipc.cpp",
        "framebuffer_capturer.cpp",
        "include/cross_semaphore.h",
        "include/framebuffer_capturer.h",
        "jni/jni_dummy_zerocopy.cpp",
    )

    assert all(
        (runtime_cpp / relative_path).is_file() for relative_path in required_files
    )
    assert (runtime_cpp / "noboost_ipc.cpp").read_bytes() == (
        REPOSITORY_ROOT / "shared-native/gl-capture/noboost_ipc.cpp"
    ).read_bytes()


def test_mc121_runtime_cmake_uses_packaged_native_sources() -> None:
    cmake = (REPOSITORY_ROOT / "minecraft/mc121/src/main/cpp/CMakeLists.txt").read_text(
        encoding="utf-8"
    )

    assert "set(GL_CAPTURE_DIR ${CMAKE_CURRENT_LIST_DIR})" in cmake
    assert "../../../../../shared-native/gl-capture" not in cmake


def test_fork_versions_have_tao_local_identifiers() -> None:
    core = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    runtime = (REPOSITORY_ROOT / "minecraft/mc121/pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert 'version = "2.7.8+tao.1"' in core
    assert 'version = "0.1.0+tao.1"' in runtime
