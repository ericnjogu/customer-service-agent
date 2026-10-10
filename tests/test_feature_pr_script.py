import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git", path="/usr/bin:/opt/homebrew/bin")
pytestmark = pytest.mark.skipif(GIT is None, reason="Git required")


@pytest.mark.parametrize("editor_result", ["success", "empty", "failure"])
@pytest.mark.parametrize("broken_git", [False, True])
def test_feature_pr_refreshes_main_and_includes_all_changes(tmp_path, editor_result, broken_git):
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    env["PATH"] = f"{tmp_path}/bin:/usr/bin:/opt/homebrew/bin:" + env["PATH"]
    env.update(
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.com",
    )

    def git(*args, cwd=tmp_path):
        return subprocess.check_output([GIT, *args], cwd=cwd, env=env, text=True).strip()

    remote = tmp_path / "remote.git"
    git("init", "--bare", "--initial-branch=main", str(remote))
    work = tmp_path / "work"
    git("clone", str(remote), str(work))
    (work / "scripts").mkdir()
    (work / "scripts/install-git-hooks.sh").write_text("#!/bin/bash\nexit 0\n")
    shutil.copy(ROOT / "scripts/feature-pr-template.txt", work / "scripts/feature-pr-template.txt")
    (work / "tracked").write_text("initial\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "initial", cwd=work)
    git("push", "origin", "main", cwd=work)
    peer = tmp_path / "peer"
    git("clone", str(remote), str(peer))
    (peer / "remote-change").write_text("upstream\n")
    git("add", ".", cwd=peer)
    git("commit", "-m", "upstream", cwd=peer)
    git("push", "origin", "main", cwd=peer)
    git("switch", "-c", "old-feature", cwd=work)
    (work / "staged").write_text("staged\n")
    git("add", "staged", cwd=work)
    (work / "tracked").write_text("unstaged\n")
    (work / "untracked").write_text("new\n")
    (tmp_path / "bin").mkdir()
    if broken_git:
        invalid_git = tmp_path / "bin/git"
        invalid_git.write_text('#!/bin/bash\necho "Bad CPU type in executable" >&2\nexit 126\n')
        invalid_git.chmod(0o755)
    gh = tmp_path / "bin/gh"
    gh.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$GH_TEST_LOG"\n')
    gh.chmod(0o755)
    env["GH_TEST_LOG"] = str(tmp_path / "gh.log")
    editor = tmp_path / "bin/test-editor"
    editor.write_text(
        "#!/bin/bash\n"
        + (
            "exit 1\n"
            if editor_result == "failure"
            else "exit 0\n"
            if editor_result == "empty"
            else 'printf "#: ignored\\nTest change\\n\\n## Summary\\nTest PR\\n" > "$1"\n'
        )
    )
    editor.chmod(0o755)
    env["GIT_EDITOR"] = str(editor)
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/create-feature-pr.sh"),
            "new-feature",
        ],
        cwd=work,
        env=env,
        check=False,
    )
    if editor_result != "success":
        assert result.returncode != 0
        assert git("branch", "--show-current", cwd=work) == "old-feature"
        assert (work / "untracked").exists()
        assert git("diff", "--cached", "--name-only", cwd=work) == "staged"
        assert "pr create" not in (tmp_path / "gh.log").read_text()
        return
    assert result.returncode == 0
    assert git("branch", "--show-current", cwd=work) == "new-feature"
    assert git("status", "--porcelain", cwd=work) == ""
    assert git("rev-parse", "HEAD^", cwd=work) == git("rev-parse", "origin/main", cwd=work)
    assert git("rev-parse", "HEAD", cwd=work) == git("rev-parse", "origin/new-feature", cwd=work)
    assert (work / "remote-change").exists()
    for name in ["staged", "tracked", "untracked"]:
        assert name in git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", cwd=work)
    assert "pr create --base main --head new-feature" in (tmp_path / "gh.log").read_text()
    assert "## Summary\nTest PR" in (tmp_path / "gh.log").read_text()
    assert "#: ignored" not in (tmp_path / "gh.log").read_text()
