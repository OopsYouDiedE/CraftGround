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


def test_mc121_runtime_contains_identical_shared_java_sources() -> None:
    shared_source = REPOSITORY_ROOT / "shared-java/src"
    packaged_source = REPOSITORY_ROOT / "minecraft/mc121/shared-java/src"
    shared_files = {
        path.relative_to(shared_source): path
        for path in shared_source.rglob("*")
        if path.is_file()
    }
    packaged_files = {
        path.relative_to(packaged_source): path
        for path in packaged_source.rglob("*")
        if path.is_file()
    }

    assert packaged_files.keys() == shared_files.keys()
    assert all(
        packaged_files[relative_path].read_bytes() == source_path.read_bytes()
        for relative_path, source_path in shared_files.items()
    )


def test_mc121_runtime_gradle_uses_packaged_shared_java_sources() -> None:
    gradle = (REPOSITORY_ROOT / "minecraft/mc121/build.gradle").read_text(
        encoding="utf-8"
    )

    assert "srcDir 'shared-java/src/main/java'" in gradle
    assert "srcDir 'shared-java/src/main/kotlin'" in gradle
    assert "../../shared-java" not in gradle


def test_mc121_cuda_capture_uses_framebuffer_attachment() -> None:
    runtime_cpp = REPOSITORY_ROOT / "minecraft/mc121/src/main/cpp"
    jni = (runtime_cpp / "jni/jni_cuda_zerocopy.cpp").read_text(encoding="utf-8")
    capturer = (runtime_cpp / "framebuffer_capturer_cuda.cpp").read_text(
        encoding="utf-8"
    )

    assert "jint colorAttachment" in jni
    assert "jint depthAttachment" in jni
    assert "glBindFramebuffer(GL_READ_FRAMEBUFFER, frameBufferId)" in jni
    assert "copyFramebufferToCudaSharedMemory(targetSizeX, targetSizeY)" in jni
    assert "glGetFramebufferAttachmentParameteriv(" in capturer
    assert "renderedTextureId" in capturer
    assert "sourceTextureId" not in capturer


def test_fork_versions_have_tao_local_identifiers() -> None:
    core = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    runtime = (REPOSITORY_ROOT / "minecraft/mc121/pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert 'version = "2.7.8+tao.2"' in core
    assert 'version = "0.1.0+tao.2"' in runtime
