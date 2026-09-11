"""Google Tasks ↔ GitHub 이슈 동기화 — 크론용.

두 방향의 위험도가 달라서 처리도 다르게 한다.

  1) 이슈 닫힘 → Task 완료      : **자동 실행**. 내 Task를 체크하는 것뿐이라 위험 없음.
  2) Task 완료 → 이슈 닫기       : **제안만**. GitHub 쓰기는 사람이 한다(madu 정책).
                                  minsfa/* 개인 레포도 지금은 제안만 — 오탐 없는 게
                                  확인되면 그때 자동으로 승격.

Task 본문(notes)이나 제목에 있는 github.com/<owner>/<repo>/issues/<n> 를 파싱해 연결한다.
이슈 하나에 Task가 여러 개 걸려 있으면 **전부 완료됐을 때만** 닫기를 제안한다.

의존: ~/.openclaw/bin/google-tasks (gog 래퍼), gh CLI
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

GOG = os.path.expanduser("~/.openclaw/bin/google-tasks")
STATE = Path(__file__).resolve().parent.parent / "data" / "tasks_github_sync.json"
ISSUE_RE = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, **kw)


def _gog_json(args):
    """gog 를 JSON 출력으로 실행. --json 미지원이면 None."""
    r = _run([GOG] + args + ["--json"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _lists():
    d = _gog_json(["tasks", "lists", "list"]) or {}
    return [(x.get("id"), x.get("title", "")) for x in d.get("tasklists", []) if x.get("id")]


def _tasks(list_id, completed=False):
    args = ["tasks", "list", list_id, "--max", "100"]
    if completed:
        args += ["--show-completed", "--show-hidden"]
    d = _gog_json(args) or {}
    return d.get("tasks", []) or []


def _refs(task):
    """Task 에서 (owner, repo, number) 집합 추출."""
    text = f"{task.get('title', '')}\n{task.get('notes', '')}"
    return {(m.group(1), m.group(2), int(m.group(3))) for m in ISSUE_RE.finditer(text)}


def _issue_state(owner, repo, num):
    r = _run(["gh", "issue", "view", str(num), "--repo", f"{owner}/{repo}",
              "--json", "state,title"])
    if r.returncode != 0:
        return None, None
    try:
        d = json.loads(r.stdout)
        return d.get("state"), d.get("title")
    except json.JSONDecodeError:
        return None, None


def main():
    if not os.path.exists(GOG):
        print(f"OK (google-tasks 없음: {GOG})")
        return

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    done_before = set(state.get("closed_task_ids", []))

    closed_tasks, propose = [], {}
    issue_cache = {}

    for list_id, list_name in _lists():
        for t in _tasks(list_id, completed=True):
            tid, title = t.get("id"), (t.get("title") or "")[:50]
            refs = _refs(t)
            if not refs:
                continue
            is_done = t.get("status") == "completed"

            for owner, repo, num in refs:
                key = f"{owner}/{repo}#{num}"
                if key not in issue_cache:
                    issue_cache[key] = _issue_state(owner, repo, num)
                istate, ititle = issue_cache[key]
                if istate is None:
                    continue

                # ① 이슈 닫힘 + Task 미완 → Task 완료 처리 (자동)
                if istate == "CLOSED" and not is_done and tid not in done_before:
                    r = _run([GOG, "tasks", "done", list_id, tid])
                    if r.returncode == 0:
                        closed_tasks.append(f"✅ Task 완료 「{title}」 ← {key} 닫힘")
                        done_before.add(tid)
                    else:
                        closed_tasks.append(f"⚠️ Task 완료 실패 「{title}」 ({key})")

                # ② Task 완료 + 이슈 열림 → 닫기 제안 (자동 실행 안 함)
                if istate == "OPEN" and is_done:
                    propose.setdefault(key, {"title": ititle, "tasks": [], "open": 0})
                    propose[key]["tasks"].append(title)

    # 같은 이슈에 걸린 미완 Task가 남아 있으면 제안에서 뺀다
    for list_id, _ in _lists():
        for t in _tasks(list_id, completed=False):
            if t.get("status") == "completed":
                continue
            for owner, repo, num in _refs(t):
                key = f"{owner}/{repo}#{num}"
                if key in propose:
                    propose[key]["open"] += 1

    lines = []
    if closed_tasks:
        lines.append("■ 이슈가 닫혀서 Task도 완료 처리했습니다")
        lines += closed_tasks
    ready = {k: v for k, v in propose.items() if v["open"] == 0}
    if ready:
        lines.append("\n■ 닫아도 될 것 같은 이슈 (연결 Task가 전부 완료됨)")
        for k, v in ready.items():
            lines.append(f"· {k} 「{(v['title'] or '')[:45]}」 ← {', '.join(v['tasks'][:3])}")
        lines.append("  → 닫으려면 지시해 주세요. 자동으로 닫지 않습니다.")
    held = {k: v for k, v in propose.items() if v["open"] > 0}
    if held:
        lines.append("\n■ 일부만 완료 (아직 닫지 않음)")
        for k, v in held.items():
            lines.append(f"· {k} — 미완 Task {v['open']}건 남음")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({
        "closed_task_ids": sorted(done_before),
        "last_run": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))

    print("\n".join(lines) if lines else "OK 동기화할 것 없음")


if __name__ == "__main__":
    main()
