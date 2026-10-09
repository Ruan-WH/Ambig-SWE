# Sealing gold-patch channels (hidden setting)

## Why

SWE-bench instance images keep the full repository history, and the dataset
record is injected into the container. Both contain the gold patch, so a high
resolve rate cannot be read as independent problem solving until those channels
are closed and an unsealed control is run on the *same* code. The historical
20-instance run predates both the seal and a prompt/tool-call change, so it is
reference material only - it is not arm A.

## What `SEAL_GOLD_LEAK=true` closes

| Channel | Handling |
|---|---|
| git history of `/testbed` | sealed **before** `instance_swe_entry.sh` copies it, so the workspace copy inherits the seal; every commit that is not an ancestor of `base_commit` is removed |
| injected `/swe_util/eval_data/instances/swe-bench-instance.json` | reduced to `instance_id`, `repo`, `version`; the key set is asserted with `jq` |
| network, pip/GitHub, model memorisation | **not** sealed - conclusions are limited to "removing local git history and the dataset record" |

## Files

- `scripts/setup/seal_gold_history.sh` - performs the seal, prints one
  `SEAL_EVIDENCE {json}` line per repo plus `SEAL_OK` / `SEAL_BROKEN`.
- `data/known_fix_commits.json` - spot-check shas, **verified against real images
  only**. An unverified entry can abort a whole run, because a configured sha
  that is absent before sealing fails closed.
- `scripts/audit/verify_fix_mappings.sh` - checks those mappings against the real
  images in throwaway containers, before spending a run.
- `scripts/audit/leak_audit.py` - per-instance leak report over recorded runs;
  `--recover-fix-commits` regenerates *candidate* shas from a trajectory.
- `hidden_run_infer.py` - `SEAL_GOLD_LEAK` flag, JSON sanitisation, probes,
  evidence persistence. `RUN_WITH_BROWSING`, `FakeUser`, `filter_dataset` and
  `API_KEY` are deliberately untouched so the seal is the only variable.

## Environment this repo is run in

| Thing | Value |
|---|---|
| inference env | `conda activate ambig-swe` (provides `poetry`; poetry uses `~/.cache/pypoetry/virtualenvs/openhands-ai-RBcQpR6Q-py3.12`) |
| grading env | `conda activate ambig-swe-grader` (Python 3.11 + `swebench`) |
| docker access | On this host `/var/run/docker.sock` is `nobody:nogroup` with mode `660`, and `rwh` belongs to `nogroup`. Check the socket owner/group and run `docker info` in the terminal that will launch the experiment; access from a restricted execution sandbox may still be denied. |
| LLM key | export `OPENAI_API_KEY`; `LLM_API_KEY` has no effect on this path (see note) |
| runner permissions | `hidden_run_infer.sh` and `base_run_infer.sh` are mode 644 - invoke with `bash`, not `./` |

**Why `OPENAI_API_KEY`.** `get_llm_config_arg()` returns the `[llm.deepseek-v4-flash]`
group straight from `config.toml` without merging `LLM_*` environment variables,
and that group sets no `api_key` (the recorded `metadata.json` shows
`"api_key": null`). `openhands/llm/llm.py` passes that `None` to LiteLLM, which
then falls back to the provider variable for the `openai/` prefix, i.e.
`OPENAI_API_KEY`. Exporting `LLM_API_KEY` leaves authentication unconfigured.

## Runbook

Commit the harness changes first: `metadata.json` records `git_commit`, and arm A
and arm B must be provably the same code. Stage only what you mean to commit -
`command.txt` is currently untracked.

```bash
# the three modified runners and the proxy script they source are part of the
# experiment code, so the recorded commit only reproduces the run if they are in
git add evaluation/benchmarks/swe_bench/hidden_run_infer.py \
        evaluation/benchmarks/swe_bench/base_run_infer.py \
        evaluation/benchmarks/swe_bench/scripts/setup/seal_gold_history.sh \
        evaluation/benchmarks/swe_bench/scripts/audit \
        evaluation/benchmarks/swe_bench/scripts/base_run_infer.sh \
        evaluation/benchmarks/swe_bench/scripts/hidden_run_infer.sh \
        evaluation/benchmarks/swe_bench/scripts/interact_run_infer.sh \
        evaluation/benchmarks/swe_bench/scripts/configure_host_proxy.sh \
        evaluation/benchmarks/swe_bench/data/known_fix_commits.json \
        evaluation/benchmarks/swe_bench/SEAL_GOLD_LEAK.md
git commit -m "seal gold-patch channels for the hidden setting"
git status --short   # command.txt must still be untracked here
```

```bash
source ~/miniconda3/etc/profile.d/conda.sh
conda activate ambig-swe
docker info >/dev/null  # stop here if the experiment terminal cannot reach Docker
export OPENAI_API_KEY=...
export DOCKER_RUNTIME_KWARGS='{"mem_limit":"6g","memswap_limit":"8g"}'

# 0. verify every mapping against the real images (seconds each; skips images
#    that are not local yet). Fix any FAIL here before running arm B.
DATASET_JSON=evaluation/evaluation_outputs/outputs/princeton-nlp__SWE-bench_Lite-test/CodeActAgent/deepseek-flash_maxiter_70_N_v0.20.0-no-hint-deepseek-v4-flash-hidden-10x70-safe-run_1/hidden-dataset-20.json
bash evaluation/benchmarks/swe_bench/scripts/audit/verify_fix_mappings.sh "$DATASET_JSON"
```

If Docker reports a proxy on `127.0.0.1`, the runner forwards it to Buildx and
uses host networking during the runtime image build. This avoids slow direct
downloads of OpenVSCode Server and Python packages. An explicit
`RUNTIME_EXTRA_BUILD_ARGS` value takes precedence. Restart a build that was
already running when this proxy configuration was changed.

To widen spot-check coverage, regenerate candidates and admit only the ones the
verifier confirms:

```bash
python evaluation/benchmarks/swe_bench/scripts/audit/leak_audit.py \
  --eval-output-dir <historical leaky run dir> --dataset-json "$DATASET_JSON" \
  --recover-fix-commits /tmp/candidates.json
# pull the instance images you want to cover, then re-run verify_fix_mappings.sh
# with those entries merged in; keep only OK ones in known_fix_commits.json
```


Smoke test on `astropy__astropy-13033` **only**. `-eval-n-limit 2` would also run
`12907`, which is slow; instead select the single instance through the
benchmark-local filter that `filter_dataset()` documents:

```bash
# temporary: filtering is picked up from evaluation/benchmarks/swe_bench/config.toml
(
SMOKE_FILTER=evaluation/benchmarks/swe_bench/config.toml
trap 'rm -f "$SMOKE_FILTER"' EXIT
printf 'selected_ids = ["astropy__astropy-13033"]\n' \
  > "$SMOKE_FILTER"

export SEAL_GOLD_LEAK=true EXP_NAME=sealed-smoke
bash evaluation/benchmarks/swe_bench/scripts/hidden_run_infer.sh \
  deepseek-v4-flash HEAD CodeActAgent "" 70 1 princeton-nlp/SWE-bench_Lite test 1

# inspect before continuing; the subshell trap removes the filter even on failure
ls evaluation/evaluation_outputs/outputs/*/CodeActAgent/*sealed-smoke*/seal_evidence/
)
```

Then arm A and arm B on the same 20 instances, same code:

```bash
unset SEAL_GOLD_LEAK; export EXP_NAME=abA-unsealed
bash evaluation/benchmarks/swe_bench/scripts/hidden_run_infer.sh \
  deepseek-v4-flash HEAD CodeActAgent 20 70 1 princeton-nlp/SWE-bench_Lite test 1

export SEAL_GOLD_LEAK=true EXP_NAME=abB-sealed
bash evaluation/benchmarks/swe_bench/scripts/hidden_run_infer.sh \
  deepseek-v4-flash HEAD CodeActAgent 20 70 1 princeton-nlp/SWE-bench_Lite test 1
```

Grade and audit each arm:

```bash
conda activate ambig-swe-grader
bash evaluation/benchmarks/swe_bench/scripts/eval_infer.sh <arm dir>/output.jsonl

conda activate ambig-swe
# a new run directory does NOT contain hidden-dataset-20.json; reuse the
# historical one, which describes the same 20 instances
python evaluation/benchmarks/swe_bench/scripts/audit/leak_audit.py \
  --eval-output-dir <arm dir> --dataset-json "$DATASET_JSON" \
  --grader-report <grader report>.json \
  --out evaluation/evaluation_outputs/analysis_artifacts/<arm>-leak-audit.json
```

## Evidence and failure semantics

Evidence lands in `<eval_output_dir>/seal_evidence/<instance_id>.json` before any
failure can drop the runtime: `HEAD`, refs, reflog/stash/replace/alternates,
reachable-vs-ancestor commit counts, unreachable commit *and* object counts, the
sanitised JSON key set, and the known-fix verdict per phase.

The known-fix verdict is one of:

| Verdict | Meaning | Result |
|---|---|---|
| `confirmed_removed` | present before sealing, unreadable after | pass |
| `still_present` | a commit carrying gold content survived | **fail** |
| `unverified_absent_before` | configured sha this image never had, so the spot-check proved nothing | **fail** - fix the mapping and re-run |
| `not_requested` | no mapping for this instance; structural probes only | pass |

Any probe failure aborts the instance; the harness retries five times and then
stops the whole run. That is intentional: a partially sealed arm is not
interpretable.

The workspace probe passes `-` on purpose: it inspects a copy of the already
sealed `/testbed`, so a known-fix check there is meaningless by construction. The
confirmation comes from the `testbed` phase.

## Common Agent-stage network isolation (A/B/C)

Hidden runs now default to `HIDDEN_AGENT_NETWORK_ISOLATION=true`, independently
of `SEAL_GOLD_LEAK` and `HIDDEN_AGENT_RECOVERY`. For the three-arm comparison,
explicitly export the same value in every arm:

```bash
export HIDDEN_AGENT_NETWORK_ISOLATION=true
# A: SEAL_GOLD_LEAK=false, HIDDEN_AGENT_RECOVERY=false
# B: SEAL_GOLD_LEAK=true,  HIDDEN_AGENT_RECOVERY=false
# C: SEAL_GOLD_LEAK=true,  HIDDEN_AGENT_RECOVERY=true
```

Image construction and runtime initialization retain network access. After
initialization, before the controller/agent starts, the host uses a trusted
Docker exec to apply IPv4/IPv6 egress rules inside that container's network
namespace. Local loopback and replies to host-initiated runtime requests remain
available. New outbound connections, previously established outbound sessions,
external DNS (including Docker's embedded resolver), and host proxy routes are
blocked. The host's model API requests are unaffected.

This currently requires a **local Docker runtime**, a private network namespace,
and no privileged container or added capabilities. Agent processes drop
`NET_ADMIN` and `NET_RAW`; they cannot clear the policy. Existing cached images
remain usable: if necessary, `iptables` is installed during setup, before the
network is restricted. That initial package installation may take extra time.

Each instance writes `network_evidence/<instance_id>.json`, including connectivity
probes through the actual runtime command channel. Missing tools, inaccessible
runtime control, or a failed isolation probe abort the instance before Agent
execution. Metadata records `hidden_agent_network_isolation`.

`HIDDEN_AGENT_NETWORK_ISOLATION=false` reproduces the earlier unrestricted-network
behavior; do not use it in this A/B/C comparison. No recovery guidance or task
prompt is added by isolation. Network-dependent repository tests/downloads may
fail during Agent execution; that restriction is shared across all three arms.
The separate grader remains unchanged and runs outside the Agent container.
