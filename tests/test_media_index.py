"""The current directory decides which indexed media still exist."""

import json

import pytest
from fastapi.testclient import TestClient

from baird_hoard import media
from baird_hoard.config import Config
from baird_hoard.main import create_app


@pytest.fixture
def library_client(tmp_path):
    profile = tmp_path / "profile"
    folder = profile / "media"
    folder.mkdir(parents=True)
    config = Config(data_dir=profile, port=17698, data_dir_configured=True, isolated_instance=True,
                    background=False)
    records = {"present.png": {"origin": "snapshot", "width": 12},
               "present.mp4": {"origin": "recording", "duration": 1.5},
               "missing.mp4": {"origin": "recording", "duration": 2},
               "removed.mp4": {"origin": "recording", "duration": 3}}
    config.index_path.write_text(json.dumps(records), encoding="utf-8")
    for name in ("present.png", "present.mp4", "removed.mp4"):
        (folder / name).write_bytes(b"synthetic media")
    app = create_app(config)
    with TestClient(app, base_url="http://127.0.0.1:17698", client=("127.0.0.1", 50000)) as client:
        (folder / "removed.mp4").unlink()
        yield client, config.index_path, {name: records[name] for name in ("present.png", "present.mp4")}


@pytest.mark.parametrize("through_agent", [False, True])
def test_listing_prunes_missing_media_across_filters(library_client, through_agent):
    client, index, expected = library_client
    if through_agent:
        token = client.app.state.services.token
        response = client.post("/api/agent/call", headers={"Authorization": f"Bearer {token}"},
                               json={"name": "media_list", "arguments": {"kind": "image", "limit": 1}})
    else:
        response = client.get("/api/media", params={"kind": "image", "limit": 1})

    assert [item["name"] for item in response.raise_for_status().json()["items"]] == ["present.png"]
    assert json.loads(index.read_text(encoding="utf-8")) == expected


def test_listing_retries_pruning_after_an_index_write_failure(library_client, monkeypatch):
    client, index, expected = library_client
    original = index.read_bytes()

    def deny_write(*args, **kwargs):
        raise PermissionError("index is temporarily unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(media, "write_json_atomic", deny_write)
        response = client.get("/api/media")
        assert {item["name"] for item in response.raise_for_status().json()["items"]} == set(expected)
        assert index.read_bytes() == original

    client.get("/api/media").raise_for_status()
    assert json.loads(index.read_text(encoding="utf-8")) == expected
