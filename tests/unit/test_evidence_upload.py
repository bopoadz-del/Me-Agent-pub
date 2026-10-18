"""Gap 3: a caller hands documents to the agent over its API.

POST /evidence/{name} takes one document as the raw request body (UTF-8 text,
the only thing the evidence reader reads) and stores it inside the evidence
whitelist, DATA_DIR/evidence, under a directory of its own. The reply names
the path to attach to a mission. The route sits behind the same bearer token
and the same request-body cap as every other route; it never widens the
whitelist and never writes outside it.
"""
from __future__ import annotations

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
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(tmp_path / "intake"))
    with TestClient(bootstrapper._create_api({"comms": None, "store": None})) as c:
        yield c, tmp_path


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_an_upload_lands_in_the_whitelist_and_the_reader_reads_it(client):
    from agent.swarm import tools

    c, tmp = client
    text = "Free water: 165 kg/m3.\nCement: 330 kg/m3.\n"
    r = c.post("/evidence/mix.txt", content=text.encode("utf-8"), headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    path = Path(body["path"])
    assert path.name == "mix.txt" and body["type"] == "txt"
    assert path.resolve().is_relative_to((tmp / "data" / "evidence").resolve())
    assert tools.file_reader(str(path)) == text


def test_an_uploaded_path_attaches_to_a_mission_unchanged(client):
    from agent.swarm import tools

    c, _tmp = client
    path = c.post("/evidence/rates.csv", content=b"a,b\n1,2\n", headers=AUTH).json()["path"]
    items = tools.intake_evidence([{"type": "csv", "path": path}], "m-1")
    assert items == [{"type": "csv", "path": str(Path(path).resolve())}]


def test_two_uploads_of_one_name_do_not_overwrite_each_other(client):
    c, _tmp = client
    first = c.post("/evidence/a.txt", content=b"one\n", headers=AUTH).json()["path"]
    second = c.post("/evidence/a.txt", content=b"two\n", headers=AUTH).json()["path"]
    assert first != second
    assert Path(first).read_text(encoding="utf-8") == "one\n"


def test_an_upload_without_the_token_is_refused_and_writes_nothing(client):
    c, tmp = client
    r = c.post("/evidence/a.txt", content=b"x\n")
    assert r.status_code == 401
    assert _files(tmp / "data") == []


@pytest.mark.parametrize("name", ["..", ".env", "%2E%2E%2Fescape.txt", "a%2Fb.txt", "a%5Cb.txt", "-x.txt", "x" * 200])
def test_a_name_that_is_not_one_plain_file_name_is_refused(client, name):
    c, tmp = client
    r = c.post(f"/evidence/{name}", content=b"x\n", headers=AUTH)
    assert r.status_code in (400, 404, 422)
    assert _files(tmp) == []


def test_a_body_that_is_not_utf8_text_is_refused(client):
    c, tmp = client
    r = c.post("/evidence/scan.txt", content=b"\xff\xfe\x00binary", headers=AUTH)
    assert r.status_code == 415
    assert _files(tmp) == []


def test_an_empty_body_is_refused(client):
    c, tmp = client
    r = c.post("/evidence/empty.txt", content=b"", headers=AUTH)
    assert r.status_code == 400
    assert _files(tmp) == []


def test_an_upload_over_the_request_cap_is_refused(client):
    from common.http_limits import REQUEST_BODY_MAX_BYTES

    c, tmp = client
    r = c.post("/evidence/big.txt", content=b"x" * (REQUEST_BODY_MAX_BYTES + 1), headers=AUTH)
    assert r.status_code == 413
    assert _files(tmp) == []
