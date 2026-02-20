"""
Gemini CLI agent for autonomous vulnerability patching.

Implements the agent interface (setup / run) using Gemini CLI
in agentic mode. Gemini reads GEMINI.md for workflow instructions,
then autonomously: analyzes the crash -> edits source -> builds via libCRS
-> tests via libCRS -> iterates -> writes final .diff to patches_dir.
"""

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("agent.gemini_cli")

_raw_model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro").strip()
GEMINI_MODEL = _raw_model.removeprefix("gemini/")

# 0 = no timeout (run until budget is exhausted)
try:
    AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "0"))
except ValueError:
    AGENT_TIMEOUT = 0

_TEMPLATE_PATH = Path(__file__).with_suffix(".md")
GEMINI_MD_TEMPLATE = _TEMPLATE_PATH.read_text()


def setup(source_dir: Path, config: dict) -> None:
    """One-time agent configuration.

    - Sets Gemini-specific env vars (GEMINI_API_KEY, GOOGLE_GEMINI_BASE_URL, etc.)
    - Writes ~/.gemini/settings.json
    - Configures LiteLLM proxy via GOOGLE_GEMINI_BASE_URL + GEMINI_API_KEY
    """
    llm_api_url = config.get("llm_api_url", "")
    llm_api_key = config.get("llm_api_key", "")
    gemini_home = Path(config.get("gemini_home", Path.home() / ".gemini"))
    gemini_home.mkdir(parents=True, exist_ok=True)

    # CRS patcher container is intentionally permissive (autonomous mode).
    os.environ["IS_SANDBOX"] = "1"

    # GEMINI_CLI_HOME tells Gemini CLI where to create its .gemini/ folder.
    os.environ["GEMINI_CLI_HOME"] = str(gemini_home.parent)

    # Disable sandboxing — we're already inside a Docker container and the
    # sandbox would try to launch another Docker container, which would fail.
    # --approval-mode=yolo auto-enables sandbox, so this must be explicit.
    os.environ["GEMINI_SANDBOX"] = "false"

    # Set cleaned model name (strips LiteLLM "gemini/" prefix if present).
    # Gemini CLI reads GEMINI_MODEL from the environment natively.
    os.environ["GEMINI_MODEL"] = GEMINI_MODEL

    if llm_api_url and llm_api_key:
        # LiteLLM proxy: GOOGLE_GEMINI_BASE_URL + GEMINI_API_KEY
        # (see docs.litellm.ai/docs/tutorials/litellm_gemini_cli)
        os.environ["GOOGLE_GEMINI_BASE_URL"] = llm_api_url
        os.environ["GEMINI_API_KEY"] = llm_api_key

        logger.info("Gemini CLI configured with LiteLLM proxy: %s", llm_api_url)
        logger.info("GEMINI_MODEL: %s", GEMINI_MODEL)
    else:
        logger.warning("No LLM API URL/key set, Gemini CLI may not work")

    # Write empty settings.json so Gemini CLI doesn't prompt for setup.
    # Sandbox is disabled via GEMINI_SANDBOX env var; auth via GEMINI_API_KEY.
    settings_path = gemini_home / "settings.json"
    settings_path.write_text("{}\n")
    settings_path.chmod(0o600)
    logger.info("Wrote Gemini CLI settings to %s", settings_path)

    # Global gitignore so GEMINI.md never leaks into patches
    global_gitignore = Path.home() / ".gitignore"
    global_gitignore.write_text("GEMINI.md\n")
    subprocess.run(
        ["git", "config", "--global", "core.excludesFile", str(global_gitignore)],
        capture_output=True,
    )

    logger.info("Agent setup complete")


def run(
    source_dir: Path,
    povs: list[tuple[Path, str]],
    harness: str,
    patches_dir: Path,
    work_dir: Path,
    *,
    language: str = "c",
    sanitizer: str = "address",
    builder: str,
    ref_diff: str | None = None,
) -> bool:
    """Launch Gemini CLI in agentic mode to autonomously fix the vulnerability.

    povs is a list of (pov_path, crash_log) tuples — variants of the same bug.
    Writes all crash logs and GEMINI.md (with concrete paths), then sends a prompt.
    Gemini CLI autonomously analyzes, edits, builds, tests, iterates, and
    writes the final .diff to patches_dir.

    Returns True if a patch file was produced in patches_dir.
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # Write each crash log to a file and build POV sections for GEMINI.md
    pov_sections = []
    for i, (pov_path, crash_log) in enumerate(povs):
        crash_log_path = work_dir / f"crash_log_{i}.txt"
        crash_log_path.write_text(crash_log)
        logger.info("Wrote crash log to %s", crash_log_path)

        pov_sections.append(
            f"- POV: `{pov_path}` — crash log: `{crash_log_path}`\n"
            f"  Test: `libCRS run-pov {pov_path} <response_dir> --harness {harness} --build-id <build_id> --builder {builder}`"
        )

    pov_list = "\n".join(pov_sections)

    # Build optional diff section for delta mode
    if ref_diff:
        diff_section = (
            "\n## Reference Diff (Delta Mode)\n\n"
            "This diff shows the code change that introduced the vulnerability:\n\n"
            f"```diff\n{ref_diff}\n```\n"
        )
    else:
        diff_section = ""

    # Write GEMINI.md with concrete paths for all POVs
    gemini_md = GEMINI_MD_TEMPLATE.format(
        language=language,
        sanitizer=sanitizer,
        work_dir=work_dir,
        harness=harness,
        patches_dir=patches_dir,
        pov_list=pov_list,
        pov_count=len(povs),
        builder=builder,
        diff_section=diff_section,
    )
    (source_dir / "GEMINI.md").write_text(gemini_md)

    prompt = (
        f"Fix the vulnerability. There are {len(povs)} POV variant(s) — "
        f"crash logs are in {work_dir}/crash_log_*.txt. See GEMINI.md for tools and POV details."
    )

    stdout_log = work_dir / "gemini_stdout.log"
    stderr_log = work_dir / "gemini_stderr.log"

    cmd = [
        "gemini",
        "-m", GEMINI_MODEL,
        "--approval-mode", "yolo",
        "-d",
        prompt,
    ]

    # Stream stdout/stderr to log files. Gemini CLI uses cwd for directory context.

    try:
        with open(stdout_log, "w") as out_f, open(stderr_log, "w") as err_f:
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=err_f,
                text=True,
                cwd=source_dir,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=AGENT_TIMEOUT or None)
                logger.info("Gemini CLI exit code: %d", proc.returncode)
            except subprocess.TimeoutExpired:
                logger.warning("Gemini CLI timed out (%ds), killing process tree", AGENT_TIMEOUT)
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(2)
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
    except Exception as e:
        logger.error("Error running Gemini CLI: %s", e)
        return False

    if proc.returncode != 0:
        logger.warning("Gemini CLI failed (rc=%d), see %s", proc.returncode, stderr_log)

    # Check if agent produced any patch files
    patches = list(patches_dir.glob("*.diff"))
    if patches:
        logger.info("Agent produced %d patch(es): %s", len(patches), [p.name for p in patches])
        return True

    logger.info("Agent did not produce a patch")
    return False
