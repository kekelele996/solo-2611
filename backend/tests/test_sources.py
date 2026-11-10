from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import httpx
import pytest

from solo_auto.errors import ApiError
from solo_auto.secrets import SecretStore
from solo_auto.settings import SourceSettings, app_paths
from solo_auto.sources.project_hub import ProjectHubClient
from solo_auto.sources.staging import StagingManager


@pytest.mark.asyncio
async def test_project_hub_cookie_query_claim_and_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLO_AUTO_DISABLE_KEYCHAIN", "1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                json={"csrf": "csrf-1", "user": {"id": "u1"}},
                headers={"Set-Cookie": "session=ok; Path=/"},
            )
        if request.url.path == "/api/v1/me/projects":
            return httpx.Response(200, json={"items": [{"id": "p1", "name": "Project"}], "total": 1})
        if request.url.path.endswith("/engineering") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e1",
                            "name": "Engineering",
                            "claim_id": "c1",
                            "zip_sha256": hashlib.sha256(b"zip").hexdigest(),
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path.endswith("/e1/claim"):
            return httpx.Response(200, json={"claim_id": "c1"})
        if request.url.path.endswith("/e1/release"):
            return httpx.Response(204)
        if request.url.path.endswith("/e1/zip"):
            return httpx.Response(200, content=b"zip")
        return httpx.Response(404, json={"error": "not found"})

    secrets = SecretStore(tmp_path / "app")
    secrets.set("projecthub.password", "pw")
    client = ProjectHubClient(
        SourceSettings(base_url="http://source.test", username="user"),
        secrets,
        transport=httpx.MockTransport(handler),
    )
    projects = await client.list_projects()
    assert projects[0]["id"] == "p1"
    page = await client.list_engineering(
        "p1",
        q="mesh",
        language="python",
        min_files=10,
        tag_ids=["t1", "t2"],
    )
    assert page["items"][0]["name"] == "Engineering"
    get_request = next(
        request for request in seen if request.url.path.endswith("/engineering") and request.method == "GET"
    )
    assert get_request.url.params["language"] == "python"
    assert get_request.url.params.get_list("tag_ids") == ["t1", "t2"]
    claim = await client.claim("p1", "e1")
    assert claim["claim_id"] == "c1"
    claim_request = next(request for request in seen if request.url.path.endswith("/e1/claim"))
    assert claim_request.headers["X-CSRF-Token"] == "csrf-1"
    await client.release("p1", "e1", "c1")
    target = tmp_path / "source.zip"
    digest = await client.download_zip("p1", "e1", target)
    assert digest == hashlib.sha256(b"zip").hexdigest()
    assert target.read_bytes() == b"zip"
    await client.close()


def test_safe_zip_extract_rejects_zip_slip(tmp_path: Path) -> None:
    manager = StagingManager(app_paths(tmp_path / "app"))
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("../escape.txt", "bad")
    with pytest.raises(ApiError, match="越界"):
        manager._safe_extract(archive, tmp_path / "source")
    assert not (tmp_path / "escape.txt").exists()


class FakeSource:
    def __init__(self, content: bytes, expected_sha: str | None = None) -> None:
        self.content = content
        self.expected_sha = expected_sha

    async def download_zip(self, project_id: str, engineering_id: str, target: Path) -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w") as zipped:
            zipped.writestr("repo/main.py", "print('ok')\n")
        return hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_stage_metadata_and_sha_mismatch(tmp_path: Path) -> None:
    paths = app_paths(tmp_path / "app")
    manager = StagingManager(paths)
    source = FakeSource(b"")
    meta = await manager.stage(source, "p1", "e1", {"name": "E", "version_id": "v1", "tags": ["t"]}, "c1")
    assert meta["claimId"] == "c1"
    assert Path(meta["sourcePath"]).joinpath("main.py").is_file()
    with pytest.raises(ApiError, match="SHA-256 不一致"):
        await manager.stage(source, "p1", "e2", {"name": "E", "zip_sha256": "0" * 64}, "c2")
