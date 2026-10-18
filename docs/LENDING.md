# Lending an agent: design

Status: slice 1 done (#12, #15, #17). Slice 2 (issue #19) built. Part A: the
hive holds grants (`hive/lending.py`); `POST /lend` and `POST
/lend/{grant_id}/revoke` take the admin role, `POST /lend/redeem` turns a code
into a borrower token for the grant's agent. Part B: the agent accepts those
tokens, with the hive's grant state taken from every heartbeat ack (see "How
the agent learns of a revocation"). Agent-held grants (slice 1) stay for
agents with no hive.

**Slice 1 differs from the design below in one way:** the grants live in the
agent's own store (`agent/lending.py`), and the agent signs and verifies its
borrower tokens itself, with its owner token as the key. That needs no hive
change and no key distribution. Moving grants to the hive, with the
asymmetric signing described under "Open decisions", is a later slice.

- Part A, built: `POST /lend` (owner only), `POST /lend/redeem` (the code
  once, for a borrower token), `POST /lend/{grant_id}/revoke` (owner only,
  advances the epoch), and the route-to-scope table, which covers every route.
- Part B, built: a borrower token opens a data route when its grant carries
  that route's scope. Uploads land in `DATA_DIR/evidence/grant-<id>/`, and a
  borrower lists and fetches only that directory. A mission must name a domain
  the grant allows, may attach only the grant's evidence, and counts against
  the quota. A borrower reads back only its own grant's missions. Tokens are
  signed with a key derived from the owner token, and a grant's domains must
  be kits the agent has loaded.

## What lending means

An owner lets another person (the borrower) give missions to the owner's
agent for a limited time and a limited set of actions, and can take that
back at any moment. The borrower never receives the owner's credentials,
and never sees the owner's evidence or missions.

## What exists today

- The agent API has one bearer token (`AGENT_API_TOKEN`). Whoever holds it
  can do everything the API offers: post missions and read every mission.
  The evidence routes in progress (upload, list, fetch) take the same token.
- The hive signs session tokens (JWT with `sub`, `role`, `exp`, `jti`). It
  lets one agent be active at a time: while one agent holds the lease, a
  login by another agent gets 409 ("single-agent limit"); the same agent
  logging in again takes a new session. Admin tokens are revoked as a group
  by advancing an epoch (`hive/auth.py` `advance_admin_epoch`).

## The grant

A grant is one row in the hive, and all of its limits are data:

| field | meaning |
|---|---|
| `grant_id` | random id |
| `agent_id` | the agent being lent |
| `borrower` | a label the owner chooses; not an identity check |
| `scopes` | subset of `mission:submit`, `mission:read`, `evidence:upload`, `evidence:read` |
| `domains` | kit domains the borrower may run (empty = none) |
| `not_before`, `expires_at` | validity window, UTC |
| `max_missions` | quota for the whole grant |
| `redeem_hash` | hash of the one-time redeem code |
| `epoch` | revocation counter for this grant |
| `revoked_at` | set when the owner revokes |

## Flow

1. **Lend.** The owner, authenticated as admin, calls `POST /lend` with
   `agent_id`, `borrower`, `scopes`, `domains`, `expires_at` and
   `max_missions`. The hive stores the grant and returns a one-time redeem
   code once. Only the code's hash is stored.
2. **Redeem.** The borrower calls `POST /lend/redeem` with the code. The hive
   checks that the code is unused, the time is inside the window, and the
   grant is not revoked. It then marks the code used and returns a borrower
   token: a JWT with `sub = grant:<grant_id>`, `role = borrower`, `scope`,
   `domains`, `epoch`, `jti`, and an `exp` no later than `expires_at`.
   Tokens are short-lived (an hour, as data) and renewed from the grant
   while it stands.
3. **Use.** The borrower calls the agent with that token. The agent accepts
   either its owner token or a valid borrower token. A route-to-scope table
   (data, one row per route) decides which scope each route needs.
   `POST /mission` also checks `domain_hint` against the grant's `domains`
   and counts against `max_missions`.
4. **Revoke.** The owner calls `POST /lend/{grant_id}/revoke`. The hive sets
   `revoked_at` and advances the grant's `epoch`. Every token with an older
   epoch is dead.

## Isolation

- A borrower's evidence lives under `DATA_DIR/evidence/grant-<grant_id>/`
  and its missions are tagged with the grant. List and read routes filter
  by the caller: a borrower sees only its own grant's evidence and missions.
  The owner sees everything.
- Grounding is unchanged. A borrower's mission can cite only the evidence
  attached to it, and only the borrower's own evidence can be attached.

## How the agent learns of a revocation

The agent verifies borrower tokens itself and must also see revocations.
It pulls the hive's revocation state (grant id, epoch, revoked) on its
existing heartbeat, and refuses a borrower token when the grant is unknown,
revoked or behind on epoch. The longest it may go without a fresh pull is a
bound in data. Past that bound it refuses borrower tokens until the next
successful pull (fail closed).

As built: the hive's heartbeat ack carries `grants`, the epoch, revoked flag
and expiry of every unexpired grant naming that agent (and no other's). The
agent replaces its record of them on each ack and refuses a hive-issued token
when its grant is absent, revoked, at another epoch, expired, or last heard
longer ago than `max_grant_state_age_seconds`. Quota is counted by the agent.

## Open decisions

- **Signing (decided in slice 2, #19).** A key pair would add a native
  dependency to the hash-locked requirements and every image. Instead the hive
  signs each borrower token with a key derived from the enrolment secret of
  the agent the grant names (`common/lending_tokens.py`). The hive and that
  agent already share the secret; the agent verifies its own borrowers'
  tokens, no other agent can, and no agent holds the hive's `HIVE_JWT_KEY`.
- **Metering (built, slice A, #22).** Every borrowed mission writes one
  append-only usage row (`agent/metering.py`): start and end, steps, tool
  calls, model calls, model tokens when the adapter reports them, an outcome
  code and whether it counted. Outcomes and whether each counts are data
  (`USAGE_OUTCOMES`): only customer-caused terminal states count, and a
  mission the agent refuses or fails gives its quota slot back.
  `GET /lend/{grant_id}/usage` shows the owner any grant and a borrower only
  its own. Slice B adds a per-grant summary and a CSV/JSON export; no payment
  provider is wired, the export is the contract.

## Tests to write first, when this is built

- A code redeems once; a second redeem is refused.
- A token outside its window, or of a revoked or epoch-advanced grant, is
  refused, and so is any token past the data-bound staleness of revocation
  state.
- Each route refuses a scope the token does not carry. The route-to-scope
  table covers every route, and a new route without a row fails the test.
- A borrower cannot list, read or attach another grant's or the owner's
  evidence, and cannot read another grant's missions.
- `domain_hint` outside the grant's domains is refused, and a mission past
  `max_missions` is refused.
