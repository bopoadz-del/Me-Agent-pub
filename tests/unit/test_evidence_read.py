"""Evidence read side: list what the agent holds, fetch one document back.

GET /evidence lists the files inside the evidence whitelist (DATA_DIR/evidence),
paged, sorted by their reference (the path relative to the whitelist). The
default and largest page size are data (INPUT_BOUNDS), not code. GET
/evidence/{ref} returns one file's bytes. A reference that does not resolve to
a file inside the whitelist -- traversal, an absolute path, a link that points
outside -- is a 404, never a read. Both routes take the same bearer token as
every other route.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from agent import bootstrapper

    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    with TestClient(bootstrapper._create_api({"comms": None, "store": None})) as c:
        yield c, tmp_path


def _upload(c, name: str, text: str) -> str:
    return c.post(f"/evidence/{name}", content=text.encode("utf-8"), headers=AUTH).json()["path"]


def test_the_list_holds_every_stored_file_sorted_by_reference(client):
    c, tmp = client
    paths = [_upload(c, "b.txt", "two\n"), _upload(c, "a.txt", "one\n")]
    root = (tmp / "data" / "evidence").resolve()
    body = c.get("/evidence", headers=AUTH).json()
    refs = [Path(p).resolve().relative_to(root).as_posix() for p in paths]
    assert [i["ref"] for i in body["items"]] == sorted(refs)
    assert body["total"] == 2 and body["offset"] == 0
    assert {i["bytes"] for i in body["items"]} == {4}
    assert all(Path(i["path"]).resolve().is_relative_to(root) for i in body["items"])


def test_the_list_pages(client):
    c, _tmp = client
    for n in range(3):
        _upload(c, f"f{n}.txt", f"{n}\n")
    every = [i["ref"] for i in c.get("/evidence", headers=AUTH).json()["items"]]
    page = c.get("/evidence", params={"offset": 1, "limit": 1}, headers=AUTH).json()
    assert [i["ref"] for i in page["items"]] == every[1:2]
    assert page["total"] == 3 and page["offset"] == 1 and page["limit"] == 1


def test_the_page_bound_is_data(client, monkeypatch):
    from common.logging import OPERATIONAL_CONSTRAINTS

    c, _tmp = client
    monkeypatch.setitem(OPERATIONAL_CONSTRAINTS, "max_evidence_list_page", 2)
    assert c.get("/evidence", params={"limit": 3}, headers=AUTH).status_code == 422
    assert c.get("/evidence", params={"limit": 2}, headers=AUTH).status_code == 200
    assert c.get("/evidence", params={"offset": -1}, headers=AUTH).status_code == 422


def test_a_listed_file_is_fetched_back_byte_for_byte(client):
    c, _tmp = client
    _upload(c, "mix.txt", "Free water: 165 kg/m3.\n")
    ref = c.get("/evidence", headers=AUTH).json()["items"][0]["ref"]
    r = c.get(f"/evidence/{ref}", headers=AUTH)
    assert r.status_code == 200 and r.content == b"Free water: 165 kg/m3.\n"


def test_both_routes_need_the_token(client):
    c, _tmp = client
    _upload(c, "a.txt", "x\n")
    ref = c.get("/evidence", headers=AUTH).json()["items"][0]["ref"]
    assert c.get("/evidence").status_code == 401
    assert c.get(f"/evidence/{ref}").status_code == 401


@pytest.mark.parametrize("ref", ["missing.txt", "..%2Fagent.db", "%2E%2E/agent.db", "%2Fetc%2Fpasswd", "upload-x/../../agent.db", "a%00b.txt"])
def test_a_reference_outside_the_whitelist_or_absent_is_a_404(client, ref):
    c, tmp = client
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    (tmp / "data" / "agent.db").write_text("secret store\n", encoding="utf-8")
    _upload(c, "a.txt", "x\n")
    r = c.get(f"/evidence/{ref}", headers=AUTH)
    assert r.status_code == 404
    assert b"secret store" not in r.content


def test_a_link_inside_the_whitelist_that_points_outside_is_neither_listed_nor_read(client):
    c, tmp = client
    outside = tmp / "outside.txt"
    outside.write_text("not evidence\n", encoding="utf-8")
    root = tmp / "data" / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, root / "link.txt")
    except OSError as exc:
        pytest.skip(f"this OS cannot create symlinks here ({exc}); runs on Linux CI")
    assert [i["ref"] for i in c.get("/evidence", headers=AUTH).json()["items"]] == []
    assert c.get("/evidence/link.txt", headers=AUTH).status_code == 404
