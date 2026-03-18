import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents import gemini_cli


def test_run_uses_prompt_flag_for_non_interactive_mode(
    monkeypatch, tmp_path: Path
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    pov_dir = tmp_path / "povs"
    bug_candidate_dir = tmp_path / "bugs"
    diff_dir = tmp_path / "diffs"
    seed_dir = tmp_path / "seeds"
    patches_dir = tmp_path / "patches"
    work_dir = tmp_path / "work"
    for path in (pov_dir, bug_candidate_dir, diff_dir, seed_dir, patches_dir):
        path.mkdir()

    monkeypatch.setattr(
        gemini_cli,
        "_load_prompt_templates",
        lambda: {
            "agents_md": "{workflow_section}\n{pov_section}\n{bug_candidate_section}\n"
            "{seed_section}\n{pre_submit_section}\n{diff_section}",
            "workflow_pov": "workflow pov",
            "workflow_static": "workflow static",
            "pov_present": "pov present",
            "bug_candidates_present": "bugs present",
            "diff_present": "diffs present",
            "seed_present": "seeds present",
            "pre_submit": "{pov_line}{diff_line}",
        },
    )
    monkeypatch.setattr(gemini_cli, "_snapshot_patch_state", lambda patches_dir: {})
    monkeypatch.setattr(gemini_cli, "_changed_patches", lambda before, patches_dir: [])
    monkeypatch.setattr(
        gemini_cli.subprocess,
        "run",
        lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": b""})(),
    )

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(list(cmd))
            self.returncode = 0
            self.pid = 123

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(gemini_cli.subprocess, "Popen", FakePopen)

    result = gemini_cli.run(
        source_dir,
        pov_dir,
        bug_candidate_dir,
        diff_dir,
        seed_dir,
        "fuzz_parse_buffer_section",
        patches_dir,
        work_dir,
        builder="inc-builder",
    )

    assert result is False
    assert len(popen_calls) == 1
    cmd = popen_calls[0]
    assert "-p" in cmd
    assert "-d" in cmd
