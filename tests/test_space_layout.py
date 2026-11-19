"""정답지: scripts/space_layout.py (design: docs-hub projects/workspace/docs-pattern/design/space-layout.md)

개념(등록표의 열쇠 · 태그 · 묶음) → 폴더 한 방향 정리. scan · plan · apply · hook · locate, 등록표 새 칸.
실행: python3 -m pytest tests/test_space_layout.py -q
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "space_layout.py"
sys.path.insert(0, str(REPO / "scripts"))


def git(*a, cwd=None):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def enc(p):  # Claude Code 의 대화 기록 폴더 이름 규칙
    return str(p).replace("/", "-").replace("_", "-").replace(".", "-")


@pytest.fixture
def world(tmp_path):
    R = tmp_path / "ORG"
    H = tmp_path / "hub"
    (H / "projects/ORG").mkdir(parents=True)
    (H / "projects/ORG/_registry.md").write_text(
        "# ORG\n\n| 프로젝트 | status | 열쇠 | 태그 | 묶음 | docs | 메모 |\n|---|---|---|---|---|---|---|\n"
        "| kb | active | `git:g/kb` | solution | kb | — | 지식 |\n"
        "| tool | paused | `space:tool` | tools | — | — | 도구 |\n"
        "| exp | active | `git:g/exp` | research | exp | — | 실험 |\n", encoding="utf-8")
    rules = tmp_path / "layout.toml"
    rules.write_text(f'[ORG]\nroot = "{R}"\n[ORG.tags]\nsolution = "solution"\nresearch = "research"\ntools = "tools"\n')
    # 옛 배치
    (R / "kb").mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=R / "kb")
    (R / "kb/a.txt").write_text("a")
    git("add", ".", cwd=R / "kb"); git("commit", "-qm", "a", cwd=R / "kb")
    git("remote", "add", "origin", "http://127.0.0.1:30080/g/kb.git", cwd=R / "kb")   # 터널 주소: 호스트는 열쇠가 아니다
    git("worktree", "add", "-q", "-b", "wt", str(R / "kb-wt"), cwd=R / "kb")
    (R / "tool").mkdir()
    (R / "tool/.space").write_text("tool\n")
    (R / "tool/run.sh").write_text("echo hi\n")
    (R / "research/exp/exp").mkdir(parents=True)                  # 이미 제자리
    git("init", "-q", cwd=R / "research/exp/exp")
    git("remote", "add", "origin", "git@gitlab.example.com:g/exp.git", cwd=R / "research/exp/exp")
    (R / "stray").mkdir()                                          # 열쇠 없음
    (R / "loose.txt").write_text("x")                              # 파일
    # 경로를 가리키는 것들
    links = tmp_path / "links"; links.mkdir()
    (links / "to-tool").symlink_to(R / "tool/run.sh")
    cron = tmp_path / "crontab.txt"
    cron.write_text(f"*/10 * * * * {R}/tool/run.sh >> /dev/null 2>&1\n")
    text = tmp_path / "mem"; text.mkdir()
    (text / "note.md").write_text(f"코드는 `{R}/kb/a.txt` 에 있다. 옆 `{R}/kbx` 는 다른 것.\n")
    cc = tmp_path / "cc"
    (cc / "projects" / enc(R / "kb")).mkdir(parents=True)
    (cc / "projects" / enc(R / "kb") / "s.jsonl").write_text("{}\n")
    # 가짜 docker: R/kb 를 마운트한 compose 컨테이너 하나
    dlog = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    insp = [{"Name": "/kb-app", "Mounts": [{"Type": "bind", "Source": f"{R}/kb/conf.ttl"}],
             "Config": {"Labels": {"com.docker.compose.project": "kb",
                                   "com.docker.compose.service": "app",
                                   "com.docker.compose.project.working_dir": f"{R}/kb",
                                   "com.docker.compose.project.config_files": f"{R}/kb/docker-compose.yml",
                                   "com.docker.compose.project.environment_file": f"{R}/kb/.env.dev"}}}]
    fake.write_text("#!/bin/sh\necho \"$@\" >> " + str(dlog) + "\n"
                    "case \"$1\" in ps) echo abc123;; inspect) cat <<'EOF'\n" + json.dumps(insp) + "\nEOF\n;; esac\n")
    fake.chmod(0o755)
    env = {**os.environ, "SPACE_LAYOUT_DOCKER": str(fake)}
    common = ["--hub", str(H), "--rules", str(rules), "--cache-dir", str(tmp_path / "cache"),
              "--link-roots", str(links), "--crontab-file", str(cron), "--text-roots", str(text),
              "--claude-dirs", str(cc)]
    return dict(R=R, H=H, tmp=tmp_path, env=env, common=common, links=links, cron=cron, text=text, cc=cc, dlog=dlog)


def run(w, *a, check=True):
    p = subprocess.run([sys.executable, str(SCRIPT), *a, *w["common"]], capture_output=True, text=True, env=w["env"])
    if check:
        assert p.returncode == 0, p.stdout + p.stderr
    return p


def test_registry_reads_new_and_old_columns(world, tmp_path):
    from hub_build import read_registry
    rows = {r["name"]: r for r in read_registry(world["H"] / "projects/ORG")}
    assert rows["kb"]["keys"] == ["git:g/kb"] and rows["kb"]["tags"] == ["solution"] and rows["kb"]["group"] == "kb"
    assert rows["tool"]["group"] == "" and rows["exp"]["status"] == "active" and rows["kb"]["note"] == "지식"
    old = tmp_path / "old"; old.mkdir()
    (old / "_registry.md").write_text("| 프로젝트 | status | 경로 | docs | 메모 |\n|---|---|---|---|---|\n| a | active | `~/x/a` | hub | 메모 |\n")
    (r,) = read_registry(old)
    assert r["path"] == "~/x/a" and r["docs"] == "hub" and r["note"] == "메모" and r["keys"] == [] and r["tags"] == []


def test_scan(world):
    out = json.loads(run(world, "scan", "ORG", "--json").stdout)
    R = world["R"]
    moves = {m["from"]: m["to"] for m in out["move"]}
    assert moves == {str(R / "kb"): str(R / "solution/kb/kb"), str(R / "kb-wt"): str(R / "solution/kb/kb-wt"),
                     str(R / "tool"): str(R / "tools/tool")}
    assert {m["space"] for m in out["move"]} == {"kb", "tool"}
    assert [o["path"] for o in out["ok"]] == [str(R / "research/exp/exp")]
    assert set(out["unknown"]) == {str(R / "stray"), str(R / "loose.txt")}
    cache = json.loads((world["tmp"] / "cache/ORG.json").read_text())
    assert cache["kb"] == sorted([str(R / "kb"), str(R / "kb-wt")]) and cache["exp"] == [str(R / "research/exp/exp")]


def test_locate_uses_cache(world):
    run(world, "scan", "ORG", "--json")
    import space_layout
    assert space_layout.locate("kb", org="ORG", cache_dir=world["tmp"] / "cache") == sorted([str(world["R"] / "kb"), str(world["R"] / "kb-wt")])
    assert space_layout.locate("nope", org="ORG", cache_dir=world["tmp"] / "cache") == []


def test_hook_line(world):
    line = run(world, "hook", "ORG").stdout.strip()
    assert line.startswith("[layout] ORG:") and "옮길 것 3" in line and "모름 2" in line and "\n" not in line


def test_plan_lists_every_impact(world):
    pf = world["tmp"] / "plan.json"
    run(world, "plan", "ORG", "--out", str(pf))
    p = json.loads(pf.read_text())
    R = world["R"]
    assert {(m["from"], m["to"]) for m in p["moves"]} == {(str(R / "kb"), str(R / "solution/kb/kb")),
                                                          (str(R / "kb-wt"), str(R / "solution/kb/kb-wt")),
                                                          (str(R / "tool"), str(R / "tools/tool"))}
    imp = p["impacts"]
    assert str(R / "kb-wt") in json.dumps(imp["worktrees"])
    assert imp["containers"] and imp["containers"][0]["name"] == "kb-app" and imp["containers"][0]["service"] == "app"
    assert [l["link"] for l in imp["symlinks"]] == [str(world["links"] / "to-tool")]
    assert len(imp["crontab"]) == 1
    assert [c["from"] for c in imp["claude_history"]] == [str(world["cc"] / "projects" / enc(R / "kb"))]
    assert [t["file"] for t in imp["text"]] == [str(world["text"] / "note.md")]


def test_apply_moves_and_repairs(world):
    pf = world["tmp"] / "plan.json"
    run(world, "plan", "ORG", "--out", str(pf))
    run(world, "apply", str(pf), "--yes")
    R = world["R"]
    assert not (R / "kb").exists() and (R / "solution/kb/kb/a.txt").exists() and (R / "tools/tool/run.sh").exists()
    st = subprocess.run(["git", "-C", str(R / "solution/kb/kb-wt"), "status", "-sb"], capture_output=True, text=True)
    assert st.returncode == 0 and st.stdout.startswith("## wt")
    assert os.readlink(world["links"] / "to-tool") == str(R / "tools/tool/run.sh")
    assert str(R / "tools/tool/run.sh") in world["cron"].read_text()
    note = (world["text"] / "note.md").read_text()
    assert f"{R}/solution/kb/kb/a.txt" in note and f"`{R}/kbx`" in note          # 이름 경계: kbx 는 그대로
    assert (world["cc"] / "projects" / enc(R / "solution/kb/kb") / "s.jsonl").exists()
    assert (world["cc"] / "projects" / enc(R / "kb") / "s.jsonl").exists()        # 원본 유지
    log = world["dlog"].read_text()
    assert "compose" in log and "--env-file" in log and f"{R}/solution/kb/kb/.env.dev" in log
    assert "--no-deps" in log and "--no-build" in log and "--force-recreate" in log and " app" in log
    maps = list((R / ".layout").glob("*-map.tsv"))
    assert len(maps) == 1 and "kb\tsolution/kb" in maps[0].read_text()
    after = json.loads(run(world, "scan", "ORG", "--json").stdout)
    assert after["move"] == []


def test_apply_reverse_restores(world):
    pf = world["tmp"] / "plan.json"
    run(world, "plan", "ORG", "--out", str(pf))
    run(world, "apply", str(pf), "--yes")
    (m,) = list((world["R"] / ".layout").glob("*-map.tsv"))
    run(world, "apply", "--reverse", str(m), "--yes")
    R = world["R"]
    assert (R / "kb/a.txt").exists() and (R / "tool/run.sh").exists()
    assert subprocess.run(["git", "-C", str(R / "kb-wt"), "status"], capture_output=True).returncode == 0
    assert os.readlink(world["links"] / "to-tool") == str(R / "tool/run.sh")


def test_apply_conflict_moves_nothing(world):
    (world["R"] / "tools/tool").mkdir(parents=True)                 # 목적지에 같은 이름
    pf = world["tmp"] / "plan.json"
    run(world, "plan", "ORG", "--out", str(pf))
    p = run(world, "apply", str(pf), "--yes", check=False)
    assert p.returncode != 0
    assert (world["R"] / "kb/a.txt").exists() and (world["R"] / "kb-wt").exists()


def test_apply_needs_yes(world):
    pf = world["tmp"] / "plan.json"
    run(world, "plan", "ORG", "--out", str(pf))
    p = run(world, "apply", str(pf), check=False)
    assert p.returncode != 0 and (world["R"] / "kb").exists()


def test_duplicate_space_name_file_is_unknown(world):
    (world["R"] / "stray/.space").write_text("nope-not-registered\n")
    out = json.loads(run(world, "scan", "ORG", "--json").stdout)
    assert str(world["R"] / "stray") in out["unknown"]


def test_selftest():
    p = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_registry_section_is_the_tag_and_dip_columns_stay_first(tmp_path):
    """분류 = 등록표의 '## <id> — <이름>' 절(DIP 의 묶음 Space). 앞 다섯 칸(이름·status·경로·docs·메모)은 DIP 파서가 위치로 읽으므로 그대로,
    열쇠 · 묶음 칸은 뒤에 붙는다. 태그 칸이 없으면 절 id 가 태그다."""
    from hub_build import read_registry
    d = tmp_path / "ORG"; d.mkdir()
    (d / "_registry.md").write_text(
        "# ORG (회사)\n\n| 프로젝트 | status | 경로 | docs | 메모 | 열쇠 | 묶음 |\n|---|---|---|---|---|---|---|\n"
        "| loose | paused | `~/ORG/loose` | — | 절 밖 | `space:loose` | — |\n\n"
        "## solution — 솔루션\n\n| 프로젝트 | status | 경로 | docs | 메모 | 열쇠 | 묶음 |\n|---|---|---|---|---|---|---|\n"
        "| kb | active | `~/ORG/solution/kb/kb` | hub | 지식 | `git:g/kb` `space:kb` | kb |\n\n"
        "## research — 연구\n\n| 프로젝트 | status | 경로 | docs | 메모 | 열쇠 | 묶음 |\n|---|---|---|---|---|---|---|\n"
        "| exp | active | `~/ORG/research/exp/exp` | — | 실험 | `git:g/exp` | exp |\n", encoding="utf-8")
    rows = {r["name"]: r for r in read_registry(d)}
    assert rows["kb"]["tags"] == ["solution"] and rows["kb"]["keys"] == ["git:g/kb", "space:kb"] and rows["kb"]["group"] == "kb"
    assert rows["kb"]["path"] == "~/ORG/solution/kb/kb" and rows["kb"]["docs"] == "hub" and rows["kb"]["note"] == "지식"
    assert rows["exp"]["tags"] == ["research"] and rows["loose"]["tags"] == [] and rows["loose"]["group"] == ""


def test_non_space_entries_inside_tag_dirs_are_not_counted(world):
    """분류 폴더 안의 Space 아닌 것(열쇠 없는 폴더 · 파일)은 그 분류의 부속이라 '모름'으로 세지 않고, 그 안으로 들어가지도 않는다.
    루트 바로 아래 흩어진 것은 여전히 모름이다."""
    R = world["R"]
    (R / "tools/misc/deep").mkdir(parents=True)
    (R / "tools/misc/deep/x.txt").write_text("x")
    (R / "tools/note.pdf").write_text("x")
    (R / "research/exp/notes").mkdir(parents=True)                  # 묶음 폴더 안의 열쇠 없는 폴더
    (R / "research/exp/data.csv").write_text("x")
    out = json.loads(run(world, "scan", "ORG", "--json").stdout)
    assert set(out["unknown"]) == {str(R / "stray"), str(R / "loose.txt")}, out["unknown"]
    assert [o["path"] for o in out["ok"]] == [str(R / "research/exp/exp")]


def test_hook_without_org_covers_every_org_in_rules(world):
    line = run(world, "hook").stdout.strip()
    assert line.startswith("[layout] ORG:") and "옮길 것 3" in line and "모름 2" in line
