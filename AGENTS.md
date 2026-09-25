# Notes for agents

Read this before changing anything. `README.md` is the user-facing prose; this
file is the rules that must survive an agent's edit.

## Gates

CI runs all of these on every push (`tests`, `smoke`, `conformance`, `docker`
workflows); run them locally before committing:

```sh
uv lock --check                  # pyproject and uv.lock must agree
uv run pytest                    # coverage floor 90
uv run ruff check
uv run pyright
./scripts/smoke.sh               # real binary, stdio and HTTP, via the Inspector
./scripts/conformance-local.sh   # MCP spec conformance, baseline below
docker build -t liseur-mcp .     # the docker workflow also starts the container
```

## Releasing

Release when a consumer's install or behaviour changes: anything under `src/`,
the dependencies or metadata in `pyproject.toml`, or the `Dockerfile`. CI,
docs, tests and `scripts/` changes get no release of their own — they ride into
the next one.

```sh
# 1. bump version in pyproject.toml, then keep the lock in step — the step that
#    is easy to miss:
uv lock
uv lock --check && uv run pytest && uv run ruff check && uv run pyright

# 2. commit, push, and wait for the four workflows to pass on that commit
# 3. tag and push; the workflow does the rest
git tag -a v0.3.1 -m "v0.3.1"
git push origin v0.3.1
```

`.github/workflows/release.yml` refuses a tag that disagrees with
`pyproject.toml`, re-runs the checks on the tagged commit, and writes notes from
the commits since the previous tag.

Levels: `feat` → minor, `fix` → patch, a breaking change or a move of the `mcp`
pin → minor while the version is 0.x. When the `mcp` pin moves, name the spec
version it negotiates in the commit body — the notes are built from commits, so
that is where the claim has to live.

## Invariants

- **A published tag is immutable.** Never move, delete or re-point one; cut the
  next patch instead. (v0.1.0 still points at the pre-mcp-2.x tree, and v0.2.0
  was re-cut only minutes after publication, before anything could consume it.)
- **`conformance-baseline.yml` must never go stale.** A listed scenario that
  starts passing fails the run: remove the entry. Never add an entry to silence
  an unexpected failure — say what changed and why in the commit.
- **`scripts/live_check.py` stays out of CI.** It shape-checks a real instance
  and needs `LISEUR_URL` plus a token; do not wire it into a workflow.
- **No secrets, tokens, hostnames or library content** in the repo, the
  workflow logs or the issues.
- **`docs/upstream-openapi.sha`** records the liseur-sync spec last reviewed
  here; the `upstream-spec` workflow files an `upstream-drift` issue when
  upstream moves. Refresh the hash and date only after re-reading the changed
  endpoints against `src/liseur_mcp/client.py`.
- **The Inspector pin is watched, not forgotten.** `scripts/smoke.sh`'s
  `INSPECTOR_VERSION` is compared weekly against npm's `latest`; a lag files an
  `upstream-drift` issue (a warning, never a red run). Bump it there and in the
  README line that names it.
