#!/usr/bin/env python3
"""space_layout.py — 개념(등록표의 열쇠 · 태그 · 묶음) → 폴더, 한 방향 정리. 설계: docs-hub projects/workspace/docs-pattern/design/space-layout.md

  space_layout.py scan  ORG [--json]        루트 폴더를 훑어 제자리 · 옮길 것 · 모름으로 나누고 캐시를 쓴다
  space_layout.py plan  ORG [--out FILE]    옮길 목록과 영향(worktree · 컨테이너 · 링크 · crontab · 대화 기록 · 글 속 경로)
  space_layout.py apply FILE --yes          승인된 계획 실행 (사전 점검 → 대응표 → mv → 고치기)
  space_layout.py apply --reverse MAP --yes 이전 apply 의 대응표를 거꾸로
  space_layout.py hook  [ORG]               어긋남이 있는 조직마다 한 줄만 (ORG 생략 = 규칙 파일의 모든 조직, 아무것도 옮기지 않는다)
  space_layout.py --selftest

열쇠: git 폴더 = git:<원격 주소의 경로 부분> · 그 밖 = space:<.space 첫 줄>. 위치는 저장하지 않고 캐시(~/.cache/space-layout/<ORG>.json)만 둔다.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

HOME = Path.home()
DEFAULT_RULES = HOME / ".config/space-layout/layout.toml"
DEFAULT_CACHE = HOME / ".cache/space-layout"
DEFAULT_HUB = HOME / "workspace/docs-hub"
SKIP_DIRS = {"node_modules", ".venv", ".git"}
TEXT_EXT = {".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".tsv"}
_FAULT = {"relative_repair": False}   # selftest 가 결함을 주입하는 자리


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def under(p, folder):
    return p == folder or p.startswith(folder.rstrip("/") + "/")


def remap(p, pairs):
    for old, new in sorted(pairs, key=lambda x: -len(x[0])):
        if p and under(p, old):
            return new + p[len(old):]
    return p


def repl(text, pairs):
    """옛 경로 → 새 경로. 이름 경계: 뒤에 [A-Za-z0-9_-] 나 "."+[A-Za-z0-9] 가 이어지면 다른 이름이다"""
    if not pairs:
        return text, 0
    d = dict(pairs)
    alt = "|".join(re.escape(o) for o, _ in sorted(pairs, key=lambda x: -len(x[0])))
    rx = re.compile(r"(?<![A-Za-z0-9_.-])(" + alt + r")(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])")
    return rx.subn(lambda m: d[m.group(1)], text)


def enc(p):  # Claude Code 대화 기록 폴더 이름 규칙
    return str(p).replace("/", "-").replace("_", "-").replace(".", "-")


# ───────────────────────── scan ─────────────────────────
def url_key(url):
    url = url.strip()
    path = urlsplit(url).path if "://" in url else (url.split(":", 1)[1] if ":" in url else url)
    path = path.strip("/")
    return "git:" + (path[:-4] if path.endswith(".git") else path)


def key_of(p):
    if (p / ".git").exists():
        r = sh(["git", "-C", str(p), "config", "--get", "remote.origin.url"])
        if r.returncode == 0 and r.stdout.strip():
            return url_key(r.stdout)
    sp = p / ".space"
    if sp.is_file():
        lines = sp.read_text(encoding="utf-8").splitlines()
        if lines and lines[0].strip():
            return "space:" + lines[0].strip()
    return None


def load_rules(rules, org):
    d = tomllib.loads(Path(rules).expanduser().read_text(encoding="utf-8"))
    return Path(os.path.expanduser(d[org]["root"])), dict(d[org].get("tags", {}))


def registry_rows(hub, org):
    from hub_build import read_registry
    return read_registry(Path(hub).expanduser() / "projects" / org)


def scan(org, rules, hub):
    root, tags = load_rules(rules, org)
    by_key = {}
    for r in registry_rows(hub, org):
        for k in r["keys"]:
            by_key.setdefault(k, r)
    tagdirs = set(tags.values())
    kids = lambda d: sorted(e for e in d.iterdir() if not e.name.startswith("."))
    found, unknown = [], []   # found = (경로, 열쇠 또는 None)

    groups = {}   # 태그 폴더 이름 → 그 분류에서 등록표가 쓰는 묶음 값
    for r in registry_rows(hub, org):
        for t in r["tags"]:
            if t in tags and r["group"]:
                groups.setdefault(tags[t], set()).add(r["group"])

    def inside(d, depth):   # 태그 폴더(depth 1) · 묶음 폴더(depth 2) 안: 열쇠 없는 것은 그 분류의 부속이다 — 세지 않고 들어가지 않는다
        for e in kids(d):
            if not e.is_dir():
                continue
            k = key_of(e)
            if k:
                found.append((e, k))
            elif depth == 1 and e.name in groups.get(d.name, ()):
                inside(e, 2)

    for e in kids(root) if root.is_dir() else []:
        if not e.is_dir():
            unknown.append(e)
            continue
        k = key_of(e)
        if k or e.name not in tagdirs:
            found.append((e, k))
        else:
            inside(e, 1)
    move, ok = [], []
    for p, k in found:
        row = by_key.get(k) if k else None
        tag = next((t for t in (row["tags"] if row else []) if t in tags), None)
        if not row or not tag:
            unknown.append(p)
            continue
        to = root / tags[tag] / row["group"] / p.name if row["group"] else root / tags[tag] / p.name
        (ok if to == p else move).append({"path": str(p), "to": str(to), "space": row["name"]})
    out = {"root": str(root),
           "move": [{"from": m["path"], "to": m["to"], "space": m["space"]} for m in move],
           "ok": [{"path": m["path"], "space": m["space"]} for m in ok],
           "unknown": sorted(str(u) for u in unknown)}
    cache = {}
    for m in move + ok:
        cache.setdefault(m["space"], []).append(m["path"])
    out["cache"] = {k: sorted(v) for k, v in cache.items()}
    return out


def write_cache(org, res, cache_dir):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    (Path(cache_dir) / f"{org}.json").write_text(json.dumps(res["cache"], ensure_ascii=False, indent=1), encoding="utf-8")


def locate(name, org, cache_dir=DEFAULT_CACHE):
    """Space 이름 → 이 기계의 폴더 목록 (scan 이 쓴 캐시에서). 없으면 []"""
    f = Path(cache_dir).expanduser() / f"{org}.json"
    try:
        return list(json.loads(f.read_text(encoding="utf-8")).get(name, []))
    except (OSError, ValueError):
        return []


# ───────────────────────── plan ─────────────────────────
def docker_cmd():
    return shlex.split(os.environ.get("SPACE_LAYOUT_DOCKER") or "docker")


def find_worktrees(moves):
    out, seen = [], set()
    for m in moves:
        if not (Path(m["from"]) / ".git").exists():
            continue
        r = sh(["git", "-C", m["from"], "worktree", "list", "--porcelain"])
        paths = [l[9:] for l in r.stdout.splitlines() if l.startswith("worktree ")] if r.returncode == 0 else []
        if len(paths) > 1 and paths[0] not in seen:
            seen.add(paths[0])
            out.append({"main": paths[0], "worktrees": paths})
    return out


def find_containers(pairs):
    d = docker_cmd()
    try:
        ids = sh(d + ["ps", "-aq"]).stdout.split()
        data = json.loads(sh(d + ["inspect", *ids]).stdout) if ids else []
    except (OSError, ValueError):
        return []
    hit = lambda p: any(under(p, o) for o, _ in pairs)
    out = []
    for c in data:
        lab = (c.get("Config") or {}).get("Labels") or {}
        cfg = [x for x in lab.get("com.docker.compose.project.config_files", "").split(",") if x]
        wd = lab.get("com.docker.compose.project.working_dir", "")
        mounts = [m["Source"] for m in c.get("Mounts", []) if m.get("Type") == "bind" and hit(m.get("Source", ""))]
        if mounts or (wd and hit(wd)) or any(hit(x) for x in cfg):
            out.append({"name": c.get("Name", "").lstrip("/"), "id": c.get("Id", ""),
                        "project": lab.get("com.docker.compose.project", ""), "service": lab.get("com.docker.compose.service", ""),
                        "working_dir": wd, "config_files": cfg, "env_file": lab.get("com.docker.compose.project.environment_file", ""),
                        "mounts": mounts})
    return out


def find_links(roots, pairs):
    out = []
    for r in roots:
        r = Path(r).expanduser()
        try:
            dev = os.stat(r).st_dev
        except OSError:
            continue

        def same_dev(p):
            try:
                return os.stat(p).st_dev == dev
            except OSError:
                return False
        for dp, dn, fn in os.walk(r):
            dn[:] = [d for d in dn if d not in SKIP_DIRS and (os.path.islink(os.path.join(dp, d)) or same_dev(os.path.join(dp, d)))]
            for n in dn + fn:
                p = os.path.join(dp, n)
                if not os.path.islink(p):
                    continue
                tgt = os.readlink(p)
                ab = tgt if os.path.isabs(tgt) else os.path.normpath(os.path.join(dp, tgt))
                nab = remap(ab, pairs)
                if nab == ab:
                    continue
                new = nab if os.path.isabs(tgt) else os.path.relpath(nab, remap(dp, pairs))
                if new != tgt:
                    out.append({"link": p, "target": tgt, "new_target": new})
    return out


def read_cron(cf):
    if cf and cf != "none":
        f = Path(cf).expanduser()
        return f.read_text(encoding="utf-8") if f.exists() else ""
    r = sh(["crontab", "-l"])
    return r.stdout if r.returncode == 0 else ""


def text_files(roots):
    for r in roots:
        r = Path(r).expanduser()
        if r.is_file():
            yield r
        elif r.is_dir():
            for dp, dn, fn in os.walk(r):
                dn[:] = [d for d in dn if not d.startswith(".")]
                yield from (Path(dp) / f for f in fn if Path(f).suffix in TEXT_EXT)


def impacts(pairs, moves, o):
    claude = []
    for d in o["claude_dirs"]:
        for old, new in pairs:
            src = Path(d).expanduser() / "projects" / enc(old)
            if src.is_dir():
                claude.append({"from": str(src), "to": str(Path(d).expanduser() / "projects" / enc(new))})
    text = []
    for f in text_files(o["text_roots"]):
        try:
            n = repl(f.read_text(encoding="utf-8"), pairs)[1]
        except (OSError, UnicodeDecodeError):
            continue
        if n:
            text.append({"file": str(f), "count": n})
    cron = [{"line": i + 1, "text": l} for i, l in enumerate(read_cron(o["crontab_file"]).splitlines()) if repl(l, pairs)[1]]
    return {"worktrees": find_worktrees(moves), "containers": find_containers(pairs), "symlinks": find_links(o["link_roots"], pairs),
            "crontab": cron, "claude_history": claude, "text": text}


def make_plan(moves, root, org, o):
    pairs = [(m["from"], m["to"]) for m in moves]
    return {"org": org, "root": str(root), "moves": moves, "impacts": impacts(pairs, moves, o)}


# ───────────────────────── apply ─────────────────────────
def apply_plan(plan, o):
    moves, imp = plan["moves"], plan["impacts"]
    pairs = [(m["from"], m["to"]) for m in moves]
    root = Path(plan["root"])
    errs = [f"원본 없음: {m['from']}" for m in moves if not os.path.lexists(m["from"])]
    errs += [f"목적지에 이미 있음: {m['to']}" for m in moves if os.path.lexists(m["to"])]
    tos = [m["to"] for m in moves]
    errs += [f"목적지 겹침: {t}" for t in set(tos) if tos.count(t) > 1]
    if errs:
        print("사전 점검 실패 — 아무것도 옮기지 않았다:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 1
    # 대응표 (되돌리기용)
    ld = root / ".layout"
    ld.mkdir(parents=True, exist_ok=True)
    stem, n = date.today().isoformat(), 1
    while (ld / f"{stem}-map.tsv").exists():
        n += 1
        stem = f"{date.today().isoformat()}-{n}"
    (ld / f"{stem}-map.tsv").write_text("".join(f"{os.path.relpath(m['from'], root)}\t{os.path.relpath(os.path.dirname(m['to']), root)}\n" for m in moves), encoding="utf-8")
    (ld / f"{stem}-paths.tsv").write_text("".join(f"{a}\t{b}\n" for a, b in pairs), encoding="utf-8")
    bad = []
    # 이동
    for m in moves:
        try:
            os.makedirs(os.path.dirname(m["to"]), exist_ok=True)
            os.rename(m["from"], m["to"])
        except OSError:
            try:
                shutil.move(m["from"], m["to"])
            except OSError as e:
                print(f"이동 실패 {m['from']}: {e} — 이미 옮긴 것은 대응표 {ld / (stem + '-map.tsv')} 로 되돌린다", file=sys.stderr)
                return 1
    # worktree 다시 잇기 (절대 경로)
    gitrun = lambda cwd, *a: sh(["git", "-C", cwd, "worktree", "repair", *a], **({"cwd": "/"} if _FAULT["relative_repair"] else {}))
    ab = (lambda p: os.path.relpath(p, "/")) if _FAULT["relative_repair"] else os.path.abspath
    for w in imp["worktrees"]:
        main = remap(w["main"], pairs)
        r = gitrun(main, *[ab(remap(p, pairs)) for p in w["worktrees"] if p != w["main"]])
        if r.returncode:
            bad.append(f"worktree repair 실패 {main}: {r.stderr.strip()}")
    for m in moves:
        if (Path(m["to"]) / ".git").is_file():
            gitrun(m["to"])
    for m in moves:
        if (Path(m["to"]) / ".git").exists() and sh(["git", "-C", m["to"], "status", "--porcelain"]).returncode:
            bad.append(f"git 상태 확인 실패 {m['to']}")
    # compose 파일 안 절대 경로 — 컨테이너를 다시 띄우기 전에 고친다(안 그러면 Docker 가 옛 경로에 빈 폴더를 만든다)
    for m in moves:
        t = Path(m["to"])
        for f in [*t.glob("*compose*.y*ml"), *t.glob("*/*compose*.y*ml")]:
            s, c = repl(f.read_text(encoding="utf-8"), pairs)
            if c:
                f.write_text(s, encoding="utf-8")
    # 컨테이너 다시 띄우기 — compose 만 실행, 그 밖은 명령만 보인다
    done = set()
    for c in imp["containers"]:
        if not (c["project"] and c["service"]):
            print(f"[수동] compose 밖 컨테이너 {c['name']}: 바인드 {c['mounts']} 가 옛 경로다. docker inspect {c['name']} 의 설정으로 새 경로를 걸어 다시 실행하라 (실행하지 않음)")
            continue
        cfg = [remap(x, pairs) for x in c["config_files"]]
        env = remap(c["env_file"], pairs)
        wd = remap(c["working_dir"], pairs)
        cmd = docker_cmd() + ["compose", "-p", c["project"]] + (["--env-file", env] if env else []) + [a for f in cfg for a in ("-f", f)] \
            + ["up", "-d", "--no-deps", "--no-build", "--force-recreate", c["service"]]
        if tuple(cmd) in done:
            continue
        done.add(tuple(cmd))
        r = sh(cmd, **({"cwd": wd} if os.path.isdir(wd) else {}))
        print("[docker] " + " ".join(cmd))
        if r.returncode:
            bad.append(f"compose 재시작 실패 {c['name']}: {r.stderr.strip()}")
    # 심볼릭 링크
    for l in imp["symlinks"]:
        link = remap(l["link"], pairs)
        if os.path.islink(link):
            os.unlink(link)
            os.symlink(l["new_target"], link)
    # crontab
    if imp["crontab"]:
        new, c = repl(read_cron(o["crontab_file"]), pairs)
        if c:
            if o["crontab_file"] and o["crontab_file"] != "none":
                Path(o["crontab_file"]).expanduser().write_text(new, encoding="utf-8")
            elif sh(["crontab", "-"], input=new).returncode:
                bad.append("crontab 쓰기 실패")
    # Claude 대화 기록 (복사, 원본 유지)
    for c in imp["claude_history"]:
        if os.path.isdir(c["from"]) and not os.path.exists(c["to"]):
            shutil.copytree(c["from"], c["to"], symlinks=True)
    # 글 속 경로
    for t in imp["text"]:
        f = Path(t["file"] if os.path.exists(t["file"]) else remap(t["file"], pairs))
        s, c = repl(f.read_text(encoding="utf-8"), pairs)
        if c:
            f.write_text(s, encoding="utf-8")
    if bad:
        print("끝났지만 확인 실패:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print(f"옮김 {len(moves)}개 · 대응표 {ld / (stem + '-map.tsv')}")
    return 0


def reverse_moves(mapf):
    mapf = Path(mapf)
    root = mapf.resolve().parent.parent
    mv = []
    for line in mapf.read_text(encoding="utf-8").splitlines():
        if "\t" in line:
            old, parent = line.split("\t", 1)
            mv.append({"from": str(root / parent / Path(old).name), "to": str(root / old), "space": ""})
    return root, mv


# ───────────────────────── CLI ─────────────────────────
def opts(a):
    hub = Path(a.hub).expanduser()
    return {"hub": hub, "rules": a.rules, "cache_dir": Path(a.cache_dir).expanduser(),
            "link_roots": a.link_roots or [str(HOME)], "crontab_file": a.crontab_file,
            "text_roots": a.text_roots or [str(HOME / "memory"), *map(str, sorted((hub / "projects").glob("*/_registry.md")))],
            "claude_dirs": a.claude_dirs or [str(HOME / d) for d in (".claude", ".claude-interx", ".claude-sapiensq")]}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    c = argparse.ArgumentParser(add_help=False)
    c.add_argument("--hub", default=str(DEFAULT_HUB))
    c.add_argument("--rules", default=str(DEFAULT_RULES))
    c.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    c.add_argument("--link-roots", nargs="+")
    c.add_argument("--crontab-file", default="none")
    c.add_argument("--text-roots", nargs="+")
    c.add_argument("--claude-dirs", nargs="+")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", parents=[c]); s.add_argument("org"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("plan", parents=[c]); s.add_argument("org"); s.add_argument("--out")
    s = sub.add_parser("apply", parents=[c]); s.add_argument("plan", nargs="?"); s.add_argument("--reverse"); s.add_argument("--yes", action="store_true")
    s = sub.add_parser("hook", parents=[c]); s.add_argument("org", nargs="?")
    a = ap.parse_args(argv)
    o = opts(a)
    if a.cmd == "hook":
        try:   # 세션 시작을 막지 않는다
            orgs = [a.org] if a.org else list(tomllib.loads(Path(o["rules"]).expanduser().read_text(encoding="utf-8")))
        except Exception:
            return 0
        for org in orgs:
            try:
                res = scan(org, o["rules"], o["hub"])
                write_cache(org, res, o["cache_dir"])
            except Exception:
                continue
            n, m = len(res["move"]), len(res["unknown"])
            if n or m:
                print(f"[layout] {org}: 옮길 것 {n} · 모름 {m} — space_layout.py plan {org}")
        return 0
    if a.cmd == "scan":
        res = scan(a.org, o["rules"], o["hub"])
        write_cache(a.org, res, o["cache_dir"])
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"{a.org} ({res['root']}): 제자리 {len(res['ok'])} · 옮길 것 {len(res['move'])} · 모름 {len(res['unknown'])}")
            for m in res["move"]:
                print(f"  옮김 [{m['space']}] {m['from']} → {m['to']}")
            for u in res["unknown"]:
                print(f"  모름 {u}")
        return 0
    if a.cmd == "plan":
        res = scan(a.org, o["rules"], o["hub"])
        write_cache(a.org, res, o["cache_dir"])
        plan = make_plan(res["move"], res["root"], a.org, o)
        text = json.dumps(plan, ensure_ascii=False, indent=1)
        if a.out:
            Path(a.out).write_text(text, encoding="utf-8")
            print(f"옮길 것 {len(plan['moves'])} · " + " · ".join(f"{k} {len(v)}" for k, v in plan["impacts"].items()) + f" → {a.out}")
        else:
            print(text)
        return 0
    # apply
    if not a.yes:
        print("apply 는 --yes 가 있어야 실행한다 (먼저 plan 의 영향 목록을 읽고 승인할 것)", file=sys.stderr)
        return 2
    if a.reverse:
        root, mv = reverse_moves(a.reverse)
        plan = make_plan(mv, root, "", o)
    elif a.plan:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    else:
        print("apply 에는 계획 파일이나 --reverse 대응표가 필요하다", file=sys.stderr)
        return 2
    return apply_plan(plan, o)


# ───────────────────────── selftest ─────────────────────────
def _git(*a, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)


def _world(tmp):
    tmp = Path(tmp)
    R, H = tmp / "ORG", tmp / "hub"
    (H / "projects/ORG").mkdir(parents=True)
    (H / "projects/ORG/_registry.md").write_text(
        "| 프로젝트 | status | 열쇠 | 태그 | 묶음 | docs | 메모 |\n|---|---|---|---|---|---|---|\n"
        "| kb | active | `git:g/kb` | solution | kb | — | 지식 |\n| tool | paused | `space:tool` | tools | — | — | 도구 |\n", encoding="utf-8")
    (tmp / "layout.toml").write_text(f'[ORG]\nroot = "{R}"\n[ORG.tags]\nsolution = "solution"\ntools = "tools"\n')
    (R / "kb").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=R / "kb")
    (R / "kb/a.txt").write_text("a")
    _git("add", ".", cwd=R / "kb"); _git("commit", "-qm", "a", cwd=R / "kb")
    _git("remote", "add", "origin", "http://127.0.0.1:30080/g/kb.git", cwd=R / "kb")
    _git("worktree", "add", "-q", "-b", "wt", str(R / "kb-wt"), cwd=R / "kb")
    (R / "tool").mkdir(); (R / "tool/.space").write_text("tool\n"); (R / "tool/run.sh").write_text("x\n")
    (R / "stray").mkdir()
    (tmp / "links").mkdir(); (tmp / "links/to-tool").symlink_to(R / "tool/run.sh")
    (tmp / "cron").write_text(f"* * * * * {R}/tool/run.sh\n")
    (tmp / "mem").mkdir(); (tmp / "mem/n.md").write_text(f"`{R}/kb/a.txt` and `{R}/kbx`\n")
    (tmp / "cc/projects" / enc(R / "kb")).mkdir(parents=True)
    argv = ["--hub", str(H), "--rules", str(tmp / "layout.toml"), "--cache-dir", str(tmp / "cache"), "--link-roots", str(tmp / "links"),
            "--crontab-file", str(tmp / "cron"), "--text-roots", str(tmp / "mem"), "--claude-dirs", str(tmp / "cc")]
    return R, argv


def _run(*a):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(list(a))
    return rc, out.getvalue()


def selftest():
    old_env = os.environ.get("SPACE_LAYOUT_DOCKER")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fake = td / "docker"
        fake.write_text("#!/bin/sh\necho \"$@\" >> " + str(td / "dlog") + "\ncase \"$1\" in ps) ;; esac\n")
        fake.chmod(0o755)
        os.environ["SPACE_LAYOUT_DOCKER"] = str(fake)
        try:
            # 정상 흐름
            R, c = _world(td / "a")
            rc, out = _run("scan", "ORG", "--json", *c)
            res = json.loads(out)
            assert {m["from"]: m["to"] for m in res["move"]} == {str(R / "kb"): str(R / "solution/kb/kb"), str(R / "kb-wt"): str(R / "solution/kb/kb-wt"),
                                                                 str(R / "tool"): str(R / "tools/tool")}, res
            assert str(R / "stray") in res["unknown"], "열쇠 없는 폴더가 모름으로 안 나옴"
            assert "옮길 것 3" in _run("hook", "ORG", *c)[1]
            assert _run("plan", "ORG", "--out", str(td / "a/plan.json"), *c)[0] == 0
            plan = json.loads((td / "a/plan.json").read_text())
            assert plan["impacts"]["worktrees"] and len(plan["impacts"]["symlinks"]) == 1 and len(plan["impacts"]["crontab"]) == 1
            assert _run("apply", str(td / "a/plan.json"), *c)[0] != 0 and (R / "kb").exists(), "--yes 없이 실행됨"
            rc, _ = _run("apply", str(td / "a/plan.json"), "--yes", *c)
            assert rc == 0 and (R / "solution/kb/kb/a.txt").exists()
            assert sh(["git", "-C", str(R / "solution/kb/kb-wt"), "status", "-sb"]).stdout.startswith("## wt"), "worktree 안 이어짐"
            assert os.readlink(td / "a/links/to-tool") == str(R / "tools/tool/run.sh")
            assert f"{R}/solution/kb/kb/a.txt" in (td / "a/mem/n.md").read_text() and f"`{R}/kbx`" in (td / "a/mem/n.md").read_text()
            assert json.loads(_run("scan", "ORG", "--json", *c)[1])["move"] == []
            (m,) = (R / ".layout").glob("*-map.tsv")
            assert _run("apply", "--reverse", str(m), "--yes", *c)[0] == 0 and (R / "kb/a.txt").exists() and (R / "tool/run.sh").exists()
            assert sh(["git", "-C", str(R / "kb-wt"), "status"]).returncode == 0
            # 결함 1: 목적지 충돌 → 아무것도 안 옮김
            R, c = _world(td / "b")
            (R / "tools/tool").mkdir(parents=True)
            _run("plan", "ORG", "--out", str(td / "b/plan.json"), *c)
            assert _run("apply", str(td / "b/plan.json"), "--yes", *c)[0] != 0 and (R / "kb/a.txt").exists() and (R / "tool/run.sh").exists(), "충돌을 못 잡음"
            # 결함 2: 등록 안 된 이름표를 가진 폴더 · 열쇠 없는 폴더 → 모름
            R, c = _world(td / "c")
            (R / "stray/.space").write_text("ghost\n")
            res = json.loads(_run("scan", "ORG", "--json", *c)[1])
            assert str(R / "stray") in res["unknown"] and all(m["from"] != str(R / "stray") for m in res["move"]), "미등록 열쇠를 옮길 것으로 셈"
            # 분류 폴더 안의 열쇠 없는 것은 부속: 모름 아님. hook 은 ORG 생략 시 모든 조직
            R, c = _world(td / "f")
            (R / "tools/misc/deep").mkdir(parents=True); (R / "tools/note.pdf").write_text("x")
            (R / "solution/kb/notes").mkdir(parents=True)
            res = json.loads(_run("scan", "ORG", "--json", *c)[1])
            assert res["unknown"] == [str(R / "stray")], "분류 폴더 안 부속을 모름으로 셈"
            assert "[layout] ORG:" in _run("hook", *c)[1], "ORG 생략 hook 이 조직을 못 돎"
            # 절 태그: 태그 칸 없이 '## solution' 절 아래에 있으면 그 절이 태그다
            R, c = _world(td / "e")
            (td / "e/hub/projects/ORG/_registry.md").write_text(
                "## solution — 솔루션\n\n| 프로젝트 | status | 경로 | docs | 메모 | 열쇠 | 묶음 |\n|---|---|---|---|---|---|---|\n"
                "| kb | active | — | — | 지식 | `git:g/kb` | kb |\n", encoding="utf-8")
            res = json.loads(_run("scan", "ORG", "--json", *c)[1])
            assert {m["from"]: m["to"] for m in res["move"]}.get(str(R / "kb")) == str(R / "solution/kb/kb"), "절 태그를 못 읽음"
            # 결함 3: 상대 경로로 부른 worktree repair → 이어지지 않아 apply 가 실패해야 한다
            R, c = _world(td / "d")
            _run("plan", "ORG", "--out", str(td / "d/plan.json"), *c)
            _FAULT["relative_repair"] = True
            try:
                assert _run("apply", str(td / "d/plan.json"), "--yes", *c)[0] != 0, "끊긴 worktree 를 못 잡음"
            finally:
                _FAULT["relative_repair"] = False
        finally:
            if old_env is None:
                os.environ.pop("SPACE_LAYOUT_DOCKER", None)
            else:
                os.environ["SPACE_LAYOUT_DOCKER"] = old_env
    print("selftest OK — scan · plan · apply · reverse · 결함 주입(충돌 · 열쇠 없음 · worktree repair)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
