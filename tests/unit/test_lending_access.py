"""Lending slice 1, part B: what a borrower token opens (issue #12).

A borrower token opens a data route only when its grant carries the scope the
route-access table names. Inside, everything is the grant's own:
  - uploads land in DATA_DIR/evidence/grant-<id>/, and a borrower lists and
    fetches only that grant's evidence;
  - a mission must name a domain the grant allows, may attach only that
    grant's evidence, and counts against the grant's mission quota;
  - a borrower reads back only its own grant's missions.
The owner token still opens everything. A revoked grant's token opens nothing.
"""
from __future__ import annotations

import json
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40
OWNER = {"Authorization": f"Bearer {TOKEN}"}
TARIFF = b"Container type: dry. Dwell hours: 24. Base rate: 10 USD/hr.\n"
MISSION = "Calculate berth fee for a dry container using attached tariff evidence"


class Queue:
    """Stands in for the comms worker: holds what POST /mission enqueues."""

    def __init__(self) -> None:
        self.profiles: dict[str, object] = {}

    def queue_depth(self) -> int:
        return len(self.profiles)

    def enqueue(self, profile) -> bool:
        self.profiles[profile.mission_id] = profile
        return True

    def is_queued(self, mission_id: str) -> bool:
        return mission_id in self.profiles

    def waiting_reason(self, mission_id: str):
        return None


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from agent import bootstrapper
    from agent.memory import LocalStore
    from agent.swarm.registry import BlockRegistry
    from common.models.schemas import BlockDef
    from domain_kits.compiler.engine import compile_sheet

    monkeypatch.setenv("AGENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EVIDENCE_INTAKE_DIRS", str(tmp_path / "intake"))
    registry = BlockRegistry()
    compiled = compile_sheet(ROOT / "domain-kits" / "sheets" / "port_ops.yaml",
                             out_dir=tmp_path / "blocks", tests_out_dir=tmp_path / "kit-tests")
    for path in compiled["blocks"]:
        registry.register(BlockDef.model_validate(json.loads(Path(path).read_text(encoding="utf-8"))))
    store = LocalStore(str(tmp_path / "data" / "agent.db"))
    runtime = {"comms": Queue(), "store": store, "registry": registry}
    with TestClient(bootstrapper._create_api(runtime)) as client:
        yield client, tmp_path


def _borrower(client, scopes=("mission:submit", "mission:read", "evidence:upload", "evidence:read"),
              domains=("port_terminal",), max_missions=5):
    lent = client.post("/lend", headers=OWNER, json={
        "borrower": "visiting team", "scopes": list(scopes), "domains": list(domains),
        "ttl_seconds": 3600, "max_missions": max_missions}).json()
    token = client.post("/lend/redeem", json={"code": lent["redeem_code"]}).json()["token"]
    return lent["grant_id"], {"Authorization": f"Bearer {token}"}


def _upload(client, auth, name="tariff.txt", body=TARIFF) -> str:
    r = client.post(f"/evidence/{name}", content=body, headers=auth)
    assert r.status_code == 200, r.text
    return r.json()["path"]


def _mission(client, auth, paths, domain="port_terminal"):
    body = {"natural_language": MISSION, "attached_evidence": [{"type": "txt", "path": p} for p in paths]}
    if domain is not None:
        body["domain_hint"] = domain
    return client.post("/mission", json=body, headers=auth)


def test_a_borrower_upload_lands_in_its_grant_directory(agent):
    client, tmp = agent
    grant_id, auth = _borrower(client)
    path = Path(_upload(client, auth)).resolve()
    assert path.is_relative_to((tmp / "data" / "evidence" / f"grant-{grant_id}").resolve())


def test_a_borrower_lists_and_fetches_only_its_own_grants_evidence(agent):
    client, _tmp = agent
    _upload(client, OWNER, "owner.txt", b"owner only\n")
    _gid, auth = _borrower(client)
    _upload(client, auth)
    mine = client.get("/evidence", headers=auth).json()["items"]
    assert [Path(i["ref"]).name for i in mine] == ["tariff.txt"]
    owner_ref = next(i["ref"] for i in client.get("/evidence", headers=OWNER).json()["items"]
                     if i["ref"].endswith("owner.txt"))
    assert client.get(f"/evidence/{owner_ref}", headers=auth).status_code == 404
    assert len(client.get("/evidence", headers=OWNER).json()["items"]) == 2


def test_a_borrower_mission_runs_on_its_own_evidence_and_reads_back(agent):
    client, _tmp = agent
    _gid, auth = _borrower(client)
    r = _mission(client, auth, [_upload(client, auth)])
    assert r.status_code == 200, r.text
    mission_id = r.json()["mission_id"]
    assert client.get(f"/mission/{mission_id}", headers=auth).status_code == 200
    assert client.get(f"/mission/{mission_id}", headers=OWNER).status_code == 200


def test_a_borrower_cannot_attach_evidence_outside_its_grant(agent):
    client, tmp = agent
    owner_path = _upload(client, OWNER, "owner.txt", b"Base rate: 10 USD/hr.\n")
    (tmp / "intake").mkdir()
    (tmp / "intake" / "t.txt").write_bytes(TARIFF)
    _gid, auth = _borrower(client)
    assert _mission(client, auth, [owner_path]).status_code == 403
    assert _mission(client, auth, [str(tmp / "intake" / "t.txt")]).status_code == 403


@pytest.mark.parametrize("domain", ["concrete_mix", None])
def test_a_borrower_mission_must_name_a_domain_the_grant_allows(agent, domain):
    client, _tmp = agent
    _gid, auth = _borrower(client)
    assert _mission(client, auth, [_upload(client, auth)], domain=domain).status_code == 403


def test_the_grant_quota_counts_missions(agent):
    client, _tmp = agent
    _gid, auth = _borrower(client, max_missions=1)
    path = _upload(client, auth)
    assert _mission(client, auth, [path]).status_code == 200
    assert _mission(client, auth, [path]).status_code == 403


def test_a_borrower_reads_only_its_own_grants_missions(agent):
    client, _tmp = agent
    owner_mission = _mission(client, OWNER, [_upload(client, OWNER)]).json()["mission_id"]
    _gid, auth = _borrower(client)
    assert client.get(f"/mission/{owner_mission}", headers=auth).status_code == 404
    _gid2, other = _borrower(client)
    theirs = _mission(client, other, [_upload(client, other)]).json()["mission_id"]
    assert client.get(f"/mission/{theirs}", headers=auth).status_code == 404


def test_a_scope_the_grant_lacks_is_refused(agent):
    client, _tmp = agent
    _gid, auth = _borrower(client, scopes=("evidence:upload",))
    path = _upload(client, auth)
    assert _mission(client, auth, [path]).status_code == 403
    assert client.get("/evidence", headers=auth).status_code == 403


def test_a_revoked_grants_token_opens_nothing(agent):
    client, _tmp = agent
    grant_id, auth = _borrower(client)
    client.post(f"/lend/{grant_id}/revoke", headers=OWNER)
    assert client.post("/evidence/a.txt", content=b"x\n", headers=auth).status_code == 401
    assert client.get("/evidence", headers=auth).status_code == 401


def test_a_token_that_is_not_a_borrower_token_is_refused(agent):
    client, _tmp = agent
    assert client.get("/evidence", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_borrower_tokens_are_signed_with_a_key_derived_from_the_owner_token(agent):
    client, _tmp = agent
    _gid, auth = _borrower(client)
    token = auth["Authorization"].split(" ", 1)[1]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, TOKEN, algorithms=["HS256"])


@pytest.mark.parametrize("domains", [[], ["no_such_kit"], ["port_terminal"] * 2])
def test_a_grant_names_known_kits_only_once_each(agent, domains):
    client, _tmp = agent
    r = client.post("/lend", headers=OWNER, json={"borrower": "b", "scopes": ["mission:submit"],
                                                   "domains": domains, "ttl_seconds": 60, "max_missions": 1})
    assert r.status_code == 422
