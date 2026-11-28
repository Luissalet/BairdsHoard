import httpx
from fastapi.testclient import TestClient

from baird_hoard.api import ui
from baird_hoard.config import Config
from baird_hoard.main import create_app


SETTINGS = "/api/settings/screen-privacy"
BASE = "http://127.0.0.1:53982"
TALOS = "http://127.0.0.1:53983"
LOCAL_HEADERS = {"origin": BASE, "sec-fetch-site": "same-origin"}


def settings_client(tmp_path):
    config = Config(data_dir=tmp_path, port=53982, talos_url=TALOS, isolated_instance=True, background=False)
    return TestClient(create_app(config), base_url=BASE, client=("127.0.0.1", 50000))


def test_settings_reads_talos_and_forwards_only_explicit_local_choices(tmp_path, monkeypatch):
    saved = {"private": False, "configured": False}
    writes = []

    def get(url, **options):
        assert url == TALOS + "/screen/api/private" and options["trust_env"] is False
        return httpx.Response(200, json=saved, request=httpx.Request("GET", url))

    def post(url, *, json, headers, **options):
        assert url == TALOS + "/screen/api/private" and options["trust_env"] is False
        assert headers == {"Origin": TALOS, "Sec-Fetch-Site": "same-origin"}
        writes.append(json)
        saved.update(private=json["private"], configured=True)
        return httpx.Response(200, json={"ok": True, **saved}, request=httpx.Request("POST", url))

    monkeypatch.setattr(ui.httpx, "get", get)
    monkeypatch.setattr(ui.httpx, "post", post)
    with settings_client(tmp_path) as client:
        state = client.get(SETTINGS).raise_for_status()
        assert state.json() == {"private": False, "configured": False, "reason": "privacy_not_chosen"}
        assert state.headers["cache-control"] == "no-store"
        assert client.post(SETTINGS, json={"private": True}).status_code == 403
        assert client.post(SETTINGS, json={"private": True}, headers={**LOCAL_HEADERS, "origin": TALOS}).status_code == 403
        headers = {**LOCAL_HEADERS, "Authorization": "Bearer agent"}
        assert client.post(SETTINGS, json={"private": True}, headers=headers).status_code == 403
        assert client.post(SETTINGS, json={"private": "false"}, headers=LOCAL_HEADERS).is_error
        assert not writes
        response = client.post(SETTINGS, json={"private": True}, headers=LOCAL_HEADERS).raise_for_status()
        assert response.json() == {"private": True, "configured": True, "reason": "privacy_blocked"}
        response = client.post(SETTINGS, json={"private": False}, headers=LOCAL_HEADERS).raise_for_status()
        assert response.json() == {"private": False, "configured": True, "reason": "policy_unbound"}
        assert writes == [{"private": True}, {"private": False}]


def test_settings_reports_unavailable_or_blocked_without_confirming_a_choice(tmp_path, monkeypatch):
    def get(url, **_options):
        return httpx.Response(409, json={"error": "module_disabled"}, request=httpx.Request("GET", url))

    def post(url, **_options):
        return httpx.Response(409, request=httpx.Request("POST", url))

    monkeypatch.setattr(ui.httpx, "get", get)
    monkeypatch.setattr(ui.httpx, "post", post)
    with settings_client(tmp_path) as client:
        response = client.get(SETTINGS)
        assert response.status_code == 503 and response.json()["code"] == "screen_privacy_unavailable"
        response = client.post(SETTINGS, json={"private": False}, headers=LOCAL_HEADERS)
        assert response.status_code == 409 and response.json()["code"] == "screen_privacy_save_blocked"
