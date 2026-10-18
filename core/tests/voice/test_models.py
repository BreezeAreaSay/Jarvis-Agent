"""Модели голоса: реестр, скачивание (MockTransport), SHA-256, докачка, безопасная распаковка, удаление."""

import asyncio
import hashlib
import io
import tarfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.settings import Settings
from jarvis.voice import models as m
from jarvis.voice.models import DownloadError, ModelManager, ModelSpec, safe_extract


def make_tar(files: dict[str, bytes], *, symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


ASR_FILES = {
    "model-x/encoder.int8.onnx": b"enc" * 100,
    "model-x/decoder.onnx": b"dec",
    "model-x/joiner.onnx": b"joi",
    "model-x/tokens.txt": b"a 0\n",
    "model-x/test_wavs/0.wav": b"RIFF",
}


def spec_for(data: bytes, *, model_id: str = "asr-x", sha: bool = True, **kw: Any) -> ModelSpec:
    digest = hashlib.sha256(data).hexdigest() if sha else ""
    fields: dict[str, Any] = {
        "kind": "asr",
        "title": "Тестовая модель",
        "url": f"https://example.test/{model_id}.tar.bz2",
        "size": len(data),
        "sha256": digest,
        "folder": model_id,
    }
    fields.update(kw)
    return ModelSpec(model_id, **fields)


class Server:
    """Отдаёт файлы по URL; умеет Range (206) и считает запросы."""

    def __init__(self, files: dict[str, bytes], *, ranges: bool = True, cut_at: int | None = None) -> None:
        self.files = files
        self.ranges = ranges
        self.cut_at = cut_at
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        data = self.files.get(str(request.url))
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng and self.ranges:
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= len(data):
                return httpx.Response(416)
            return httpx.Response(206, content=data[start:])
        if self.cut_at is not None:
            cut, self.cut_at = self.cut_at, None
            return httpx.Response(200, content=data[:cut], headers={"content-length": str(cut)})
        return httpx.Response(200, content=data)


def manager(
    tmp_path: Path, specs: tuple[ModelSpec, ...], server: Server, events: list | None = None
) -> ModelManager:
    def publish(type_: str, data: dict[str, Any]) -> None:
        if events is not None:
            events.append((type_, data))

    return ModelManager(
        publish=publish,
        root=tmp_path / "models",
        transport=httpx.MockTransport(server),
        specs=specs,
        throttle_s=0.0,
        space_factor=0.0,
    )


def test_registry_is_consistent() -> None:
    assert {"silero-vad", "vosk-small", "gigaam-v3", "gigaam-v3-punct", "piper-ruslan"} <= set(m.MODELS)
    for spec in m.SPECS:
        assert spec.url.startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/")
        assert spec.size > 0
        assert spec.sha256 == "" or len(spec.sha256) == 64
        assert spec.kind in ("asr", "stream", "vad", "tts", "whisper")
    assert m.MODELS["gigaam-v3"].sha256.startswith("20a43949")
    assert m.MODELS["piper-irina"].sha256 == ""  # известен только размер
    # папки — как у jarvis-v1: уже скачанное не качается заново
    assert m.MODELS["silero-vad"].folder == "vad" and m.MODELS["piper-ruslan"].folder == "tts-ruslan"


def test_required_ids_follow_settings() -> None:
    s = Settings()
    assert ModelManager.required_ids(s) == ["silero-vad", "vosk-small", "gigaam-v3", "piper-ruslan"]
    s = Settings.model_validate({"voice": {"asr_model": "vosk-small"}, "tts": {"piper_voice": "denis"}})
    assert ModelManager.required_ids(s) == ["silero-vad", "vosk-small", "piper-denis"]


async def test_download_archive_installs_atomically(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    events: list = []
    mgr = manager(tmp_path, (spec,), Server({spec.url: data}), events)
    installed: list[str] = []
    mgr.add_listener(lambda event, model_id: installed.append(f"{event}:{model_id}"))
    assert not mgr.installed("asr-x")
    await mgr.download("asr-x")
    folder = tmp_path / "models" / "asr-x"
    assert mgr.installed("asr-x")
    assert (folder / "encoder.int8.onnx").read_bytes() == ASR_FILES["model-x/encoder.int8.onnx"]
    assert not (folder / "test_wavs").exists()  # мусор из архива убран
    assert not list((tmp_path / "models").glob(".unpack-*"))
    assert not (tmp_path / "models" / ".download" / "asr-x.part").exists()
    assert installed == ["installed:asr-x"]
    downloads = [d for t, d in events if t == "model.download"]
    assert downloads[0] == {"id": "asr-x", "progress": 0.0, "done": False}
    assert downloads[-1] == {"id": "asr-x", "progress": 1.0, "done": True}
    info = mgr.info("asr-x", required=True)
    assert info["installed"] and info["required"] and not info["downloading"] and info["progress"] == 1.0


async def test_download_single_file(tmp_path: Path) -> None:
    data = b"onnx" * 50
    spec = spec_for(data, model_id="vad-x", kind="vad", folder="vad", filename="silero_vad.onnx")
    mgr = manager(tmp_path, (spec,), Server({spec.url: data}))
    await mgr.download("vad-x")
    assert (tmp_path / "models" / "vad" / "silero_vad.onnx").read_bytes() == data
    assert mgr.installed("vad-x")


async def test_sha_mismatch_rejected(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    tampered = data[:-1] + bytes([data[-1] ^ 0xFF])
    events: list = []
    mgr = manager(tmp_path, (spec,), Server({spec.url: tampered}), events)
    with pytest.raises(DownloadError, match="SHA-256"):
        await mgr.download("asr-x")
    assert not mgr.installed("asr-x")
    assert not (tmp_path / "models" / ".download" / "asr-x.part").exists()
    last = [d for t, d in events if t == "model.download"][-1]
    assert last["done"] is True and "SHA-256" in last["error"]
    assert "SHA-256" in mgr.info("asr-x")["error"]


async def test_size_mismatch_and_http_error(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data, sha=False)
    bigger = ModelSpec(**{**spec.__dict__, "size": len(data) + 10})
    mgr = manager(tmp_path, (bigger,), Server({spec.url: data}, ranges=False))
    with pytest.raises(DownloadError, match="связь оборвалась"):
        await mgr.download("asr-x")
    mgr404 = manager(tmp_path / "b", (spec,), Server({}))
    with pytest.raises(DownloadError, match="404"):
        await mgr404.download("asr-x")


async def test_resume_with_range(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    server = Server({spec.url: data}, cut_at=len(data) // 2)
    mgr = manager(tmp_path, (spec,), server)
    with pytest.raises(DownloadError, match="оборвалась"):
        await mgr.download("asr-x")
    part = tmp_path / "models" / ".download" / "asr-x.part"
    assert part.stat().st_size == len(data) // 2  # половина сохранена
    await mgr.download("asr-x")
    assert mgr.installed("asr-x")
    assert server.requests[-1].headers["range"] == f"bytes={len(data) // 2}-"


async def test_resume_when_server_ignores_range(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    tmp = tmp_path / "models" / ".download"
    tmp.mkdir(parents=True)
    (tmp / "asr-x.part").write_bytes(b"garbage")
    mgr = manager(tmp_path, (spec,), Server({spec.url: data}, ranges=False))
    await mgr.download("asr-x")  # сервер отдал всё с начала — файл переписан, SHA сошлась
    assert mgr.installed("asr-x")


async def test_concurrent_requests_share_one_download(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    server = Server({spec.url: data})
    mgr = manager(tmp_path, (spec,), server)
    t1 = mgr.start("asr-x")
    assert mgr.start("asr-x") is t1 and mgr.downloading("asr-x")
    await asyncio.gather(mgr.download("asr-x"), mgr.download("asr-x"))
    assert len(server.requests) == 1


async def test_ensure_reports_errors(tmp_path: Path) -> None:
    good = make_tar(ASR_FILES)
    ok_spec = spec_for(good, model_id="ok")
    bad_spec = spec_for(b"x", model_id="bad", url="https://example.test/missing.tar.bz2")
    mgr = manager(tmp_path, (ok_spec, bad_spec), Server({ok_spec.url: good}))
    errors = await mgr.ensure(["bad", "ok"])
    assert list(errors) == ["bad"] and mgr.installed("ok")


async def test_archive_without_model_files_rejected(tmp_path: Path) -> None:
    data = make_tar({"model-x/README.md": b"nothing here"})
    spec = spec_for(data)
    mgr = manager(tmp_path, (spec,), Server({spec.url: data}))
    with pytest.raises(DownloadError, match="нет нужных файлов"):
        await mgr.download("asr-x")
    assert not (tmp_path / "models" / "asr-x").exists()


async def test_reinstall_replaces_folder_and_delete(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    mgr = manager(tmp_path, (spec,), Server({spec.url: data}))
    folder = tmp_path / "models" / "asr-x"
    folder.mkdir(parents=True)
    (folder / "old.txt").write_text("старое")
    await mgr.download("asr-x")
    assert not (folder / "old.txt").exists() and mgr.installed("asr-x")
    seen: list[str] = []
    mgr.add_listener(lambda event, model_id: seen.append(event))
    await mgr.delete("asr-x")
    assert not folder.exists() and not mgr.installed("asr-x")
    assert seen == ["deleting", "deleted"]


async def test_delete_cancels_running_download(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data)
    gate = asyncio.Event()

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield data[:10]
            await gate.wait()
            yield data[10:]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=SlowStream())

    events: list[dict[str, Any]] = []
    mgr = ModelManager(
        publish=lambda t, d: events.append(d),
        root=tmp_path / "models",
        transport=httpx.MockTransport(handler),
        specs=(spec,),
        space_factor=0.0,
    )
    task = mgr.start("asr-x")
    await asyncio.sleep(0.05)
    assert mgr.downloading("asr-x")
    await mgr.delete("asr-x")
    assert task.cancelled() and not mgr.downloading("asr-x")
    assert events[-1] == {"id": "asr-x", "progress": 0.0, "done": True, "error": "Скачивание отменено"}
    assert mgr.info("asr-x")["error"] == ""  # удалено человеком — ошибки нет
    assert not (tmp_path / "models" / ".download" / "asr-x.part").exists()


async def test_not_enough_disk_space(tmp_path: Path) -> None:
    data = make_tar(ASR_FILES)
    spec = spec_for(data, size=10**15)
    mgr = ModelManager(root=tmp_path / "models", transport=httpx.MockTransport(Server({})), specs=(spec,))
    with pytest.raises(DownloadError, match="места на диске"):
        await mgr.download("asr-x")


def test_unknown_model() -> None:
    from jarvis.services import UserFacingError

    with pytest.raises(UserFacingError):
        ModelManager().spec("nope")


@pytest.mark.parametrize(
    "bad",
    [
        {"/etc/passwd": b"x"},
        {"../escape.txt": b"x"},
        {"model/../../escape.txt": b"x"},
        {"C:/Windows/evil.dll": b"x"},
    ],
)
def test_safe_extract_rejects_bad_paths(tmp_path: Path, bad: dict[str, bytes]) -> None:
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(make_tar(bad))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(DownloadError, match="недопустимый путь"):
        safe_extract(archive, dest)
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_link_outside(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(make_tar({"m/a.txt": b"x"}, symlinks={"m/link": "../../outside"}))
    with pytest.raises(DownloadError, match="ссылка наружу"):
        safe_extract(archive, tmp_path / "out")


def test_safe_extract_allows_inner_link(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.bz2"
    archive.write_bytes(make_tar({"m/data/a.txt": b"x"}, symlinks={"m/link": "data/a.txt"}))
    safe_extract(archive, tmp_path / "out")
    assert (tmp_path / "out" / "m" / "data" / "a.txt").read_bytes() == b"x"


def test_file_pickers(tmp_path: Path) -> None:
    folder = tmp_path / "asr"
    folder.mkdir()
    for name in ("encoder.onnx", "encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt"):
        (folder / name).write_bytes(b"x")
    files = m.transducer_files(folder)
    assert files is not None and files["encoder"].name == "encoder.int8.onnx"
    voice = tmp_path / "tts"
    (voice / "espeak-ng-data").mkdir(parents=True)
    for name in ("ru_RU-ruslan-medium.onnx", "ru_RU-ruslan-medium.int8.onnx", "tokens.txt"):
        (voice / name).write_bytes(b"x")
    piper = m.piper_files(voice)
    assert piper is not None and piper["model"].name == "ru_RU-ruslan-medium.onnx"  # fp32 быстрее int8
    whisper = tmp_path / "wh"
    whisper.mkdir()
    for name in ("base-encoder.int8.onnx", "base-decoder.int8.onnx", "base-encoder.onnx", "base-tokens.txt"):
        (whisper / name).write_bytes(b"x")
    wf = m.whisper_files(whisper)
    assert wf is not None and wf["encoder"].name == "base-encoder.int8.onnx"
    assert m.transducer_files(tmp_path / "missing") is None


def test_auto_download_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    class Ctx:
        class platform:
            name = "fake"

    monkeypatch.delenv("JARVIS_VOICE_AUTODOWNLOAD", raising=False)
    assert m.auto_download_allowed(Ctx()) is False  # type: ignore[arg-type]
    Ctx.platform.name = "windows"
    assert m.auto_download_allowed(Ctx()) is True  # type: ignore[arg-type]
    monkeypatch.setenv("JARVIS_VOICE_AUTODOWNLOAD", "0")
    assert m.auto_download_allowed(Ctx()) is False  # type: ignore[arg-type]
    assert m.auto_download_allowed(Ctx(), True) is True  # type: ignore[arg-type]
