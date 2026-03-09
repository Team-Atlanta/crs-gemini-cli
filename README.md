# crs-gemini-cli

A [CRS](https://github.com/oss-crs) (Cyber Reasoning System) that uses [Gemini CLI](https://github.com/google-gemini/gemini-cli) to autonomously find and patch vulnerabilities in open-source projects.

Given proof-of-vulnerability (POV) inputs that crash a target binary, the agent analyzes the crashes, edits source code, builds, tests, iterates, and submits a verified patch — all autonomously.

## How it works

```
┌─────────────────────────────────────────────────────────────────────┐
│ patcher.py (orchestrator)                                           │
│                                                                     │
│  1. Fetch POVs & source         2. Reproduce crashes                │
│     crs.fetch(POV)                 libCRS run-pov (build-id: base)  │
│     crs.download(src)              → crash_log_*.txt                │
│         │                                │                          │
│         ▼                                ▼                          │
│  3. Launch Gemini CLI agent with crash logs + GEMINI.md             │
│     gemini -m <model> --approval-mode yolo -d <prompt>              │
└─────────┬───────────────────────────────────────────────────────────┘
          │ -d: prompt with crash log paths
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Gemini CLI (autonomous agent)                                       │
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────────┐                   │
│  │ Analyze  │───▶│   Fix    │───▶│   Verify     │                   │
│  │          │    │          │    │              │                   │
│  │ Read     │    │ Edit src │    │ apply-patch  │──▶ Builder        │
│  │ crash    │    │ git diff │    │   -build     │    sidecar        │
│  │ logs     │    │          │    │              │◀── build_id       │
│  └──────────┘    └──────────┘    │ run-pov ────│──▶ Builder        │
│                                  │   (all POVs)│◀── pov_exit_code  │
│                       ▲          │ run-test ───│──▶ Builder        │
│                       │          │             │◀── test_exit_code  │
│                       │          └──────┬───────┘                   │
│                       │                 │                           │
│                       └── retry ◀── fail?                           │
│                                         │ pass                      │
│                                         ▼                           │
│                              Write .diff to /patches/               │
└─────────────────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────┐
│ Submission daemon        │
│ watches /patches/ ──────▶ oss-crs framework (auto-submit)
└─────────────────────────┘
```

1. **`run_patcher`** fetches POVs and source, reproduces all crashes against the unpatched binary via the builder sidecar.
2. All POVs are batched as variants of the same vulnerability and handed to **Gemini CLI** in a single session with crash logs and `GEMINI.md` instructions.
3. The agent autonomously analyzes crash logs, edits source, and uses **libCRS** tools (`apply-patch-build`, `run-pov`, `run-test`) to build and test patches through the builder sidecar — iterating until all POV variants pass.
4. A verified `.diff` is written to `/patches/`, where a daemon auto-submits it to the oss-crs framework.

The agent is language-agnostic — it edits source and generates diffs while the builder sidecar handles compilation. The sanitizer type (`address` only in this CRS) is passed to the agent for context.

## Project structure

```
patcher.py             # Patcher module: scan POVs → agent
pyproject.toml         # Package config (run_patcher entry point)
bin/
  compile_target       # Builder phase: compiles the target project
agents/
  gemini_cli.py        # Gemini CLI agent (default)
  gemini_cli.md        # GEMINI.md template with libCRS tool docs
  template.py          # Stub for creating new agents
oss-crs/
  crs.yaml             # CRS metadata (supported languages, models, etc.)
  example-compose.yaml # Example crs-compose configuration
  base.Dockerfile      # Base image: Ubuntu + Node.js + Gemini CLI + Python
  builder.Dockerfile   # Build phase image
  patcher.Dockerfile   # Run phase image
  docker-bake.hcl      # Docker Bake config for the base image
  sample-litellm-config.yaml  # LiteLLM proxy config template
```

## Prerequisites

- **[oss-crs](https://github.com/oss-crs/oss-crs)** — the CRS framework (`crs-compose` CLI)

Builder sidecars for incremental builds are declared in `oss-crs/crs.yaml` (`snapshot: true` / `run_snapshot: true`) and handled automatically by the framework — no separate builder setup is needed.

## Quick start

### 1. Configure `crs-compose.yaml`

Copy `oss-crs/example-compose.yaml` and update the paths:

```yaml
crs-gemini-cli:
  source:
    local_path: /path/to/crs-gemini-cli
  cpuset: "2-7"
  memory: "16G"
  llm_budget: 10
  additional_env:
    CRS_AGENT: gemini_cli
    GEMINI_MODEL: gemini-3-pro-preview

llm_config:
  litellm_config: /path/to/sample-litellm-config.yaml
```

### 2. Configure LiteLLM

Copy `oss-crs/sample-litellm-config.yaml` and set your API credentials. The LiteLLM proxy routes Gemini CLI's API calls to the Google AI API (or your preferred provider). All models in `required_llms` must be configured.

### 3. Run with oss-crs

```bash
crs-compose up -f crs-compose.yaml
```

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CRS_AGENT` | `gemini_cli` | Agent module name (maps to `agents/<name>.py`) |
| `GEMINI_MODEL` | `gemini-3-pro-preview` | Model passed to `gemini -m` (strips `gemini/` prefix if present) |
| `AGENT_TIMEOUT` | `0` (no limit) | Agent timeout in seconds (0 = run until budget exhausted) |
| `BUILDER_MODULE` | `inc-builder-asan` | Builder sidecar module name (must match a `run_snapshot` entry in crs.yaml) |

Available models:
- `gemini-3-pro-preview`
- `gemini-3-flash-preview`

## Runtime behavior

- **Execution**: `gemini --approval-mode yolo` (auto-approve all tool use)
- **Instruction file**: `GEMINI.md` generated per run in the target repo
- **LiteLLM proxy**: Configured via `GOOGLE_GEMINI_BASE_URL` + `GEMINI_API_KEY` env vars

Debug artifacts:
- Shared directory: `/root/.gemini` (registered as `gemini-home`)
- Per-run logs: `/work/agent/gemini_stdout.log`, `/work/agent/gemini_stderr.log`

## Patch validity

A patch is submitted only when it meets all criteria:

1. **Builds** — compiles successfully
2. **POVs don't crash** — all POV variants pass
3. **Tests pass** — project test suite passes (or skipped if none exists)
4. **Semantically correct** — fixes the root cause with a minimal patch

Submission is final once a `.diff` is written to `/patches/` and picked up by the watcher. Submitted patches cannot be edited or resubmitted, so complete a full pre-submit review first.

## Adding a new agent

1. Copy `agents/template.py` to `agents/my_agent.py`.
2. Implement `setup()` and `run()`.
3. Set `CRS_AGENT=my_agent`.

The agent receives:
- **source_dir** — clean git repo of the target project
- **povs** — list of POV file paths (may be empty)
- **bug_candidates** — list of static finding files (SARIF/JSON/text; may be empty)
- **harness** — harness name for `run-pov`
- **patches_dir** — write verified `.diff` files here
- **work_dir** — scratch space
- **language** — target language (c, c++, jvm)
- **sanitizer** — sanitizer type (`address` only)
- **builder** — builder sidecar module name (keyword-only, required)
- **ref_diff** — reference diff showing the bug-introducing change (delta mode only, None in full mode)

The agent has access to three libCRS commands (the `--builder` flag specifies which builder sidecar module to use):
- `libCRS apply-patch-build <patch.diff> <response_dir> --builder <module>` — build a patch
- `libCRS run-pov <pov> <response_dir> --harness <h> --build-id <id> --builder <module>` — test against a POV
- `libCRS run-test <response_dir> --build-id <id> --builder <module>` — run the project's test suite

For transparent diagnostics, always inspect response_dir logs:
- Build: `build.log`, `build_stdout.log`, `build_stderr.log`
- POV: `pov_stdout.log`, `pov_stderr.log`
- Test: `test_stdout.log`, `test_stderr.log`
