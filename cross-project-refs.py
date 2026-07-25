"""Measure cross-project reference in Claude Code sessions.

Answers: when a session in project A reaches into project B, does it find what
it needs, or does it hunt? Run `python3 cross-project-refs.py [days]`.

Folds worktree siblings (apps/rally-hq-wt-reel, wip/film-room-wt-golden) back
onto their base project — those are the same codebase in another checkout, and
counting them as cross-project inflates the rate by ~4 points.

BASELINE 2026-07-24 (90-day window), recorded so the "revisit if it gets worse"
bar is checkable rather than aspirational:
    640 sessions with workspace tool activity
    272 (42.5%) referenced another project
    median 2 search calls before finding the file  <- the common case works
    p90 17; 91/272 needed 5+ (but see caveat below)
    63 high-flail sessions, landing targets flat (5,5,2,2,2,2,2,1,1,...) —
       no repeat target, so an index has nothing to index
    top route apps/letspepper <-> apps/rally-hq: 193 distinct files over 294
       reads — parallel demo-reel feature work, not canonical-primitive lookup

CAVEAT on the flail metric: Bash counts as a search tool, so `ls`/`git log`/`du`
against an outside path register as hunting. A session deliberately comparing
two projects also looks high-flail. The number is an upper bound on real misses.

CONCLUSION: no shared substrate warranted. working-style.md's internal reference
map is @-included into every session — already in context at zero retrieval
cost. A separate index would need a retrieval step that doesn't exist.
"""
import json, os, re, sys, time, collections
ROOT = os.path.expanduser('~/Workspace/dev')
PROJ = os.path.expanduser('~/.claude/projects')
DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
cutoff = time.time() - DAYS*86400

# a "project" is the 2-segment path under ~/Workspace/dev (apps/rally-hq, tools/blueprint)
def proj_of(path):
    if not path.startswith(ROOT): return None
    rest = path[len(ROOT):].strip('/').split('/')
    if not rest or not rest[0]: return None
    if rest[0] in ('apps','tools','wip','client','ref','archive') and len(rest) > 1:
        return f"{rest[0]}/{rest[1]}"
    return rest[0]

# Real project dirs on disk. Used to fold worktree siblings (apps/rally-hq-wt,
# wip/film-room-wt-golden, apps/photography-vnext-p1) back onto their base
# project — those are the SAME codebase in another checkout, and counting them
# as cross-project reference inflates the rate badly.
REAL = set()
for _grp in ('apps', 'tools', 'wip', 'client', 'ref', 'archive'):
    _gp = os.path.join(ROOT, _grp)
    if os.path.isdir(_gp):
        for _n in os.listdir(_gp):
            if os.path.isdir(os.path.join(_gp, _n)):
                REAL.add(f"{_grp}/{_n}")

def normalize(p):
    if p is None or p in REAL:
        return p
    cands = [r for r in REAL if p.startswith(r + '-') or p.startswith(r + '.')]
    return max(cands, key=len) if cands else p

PATH_RE = re.compile(r'/Users/nino/Workspace/dev/[A-Za-z0-9._/-]+')
SEARCH_TOOLS = {'Grep','Glob'}
READ_TOOLS = {'Read'}

sessions = 0
xproj_sessions = 0
target_hits = collections.Counter()
pair_hits = collections.Counter()
flail_costs = []
per_session_refs = []

for d in os.listdir(PROJ):
    dp = os.path.join(PROJ, d)
    if not os.path.isdir(dp): continue
    for fn in os.listdir(dp):
        if not fn.endswith('.jsonl'): continue
        fp = os.path.join(dp, fn)
        try:
            if os.path.getmtime(fp) < cutoff: continue
        except OSError: continue

        home = None
        events = []   # (kind, project) in order
        try:
            with open(fp, errors='replace') as f:
                for line in f:
                    if '"tool_use"' not in line and '"cwd"' not in line: continue
                    try: obj = json.loads(line)
                    except Exception: continue
                    if home is None and obj.get('cwd'):
                        home = normalize(proj_of(obj['cwd']))
                    msg = obj.get('message') or {}
                    content = msg.get('content')
                    if not isinstance(content, list): continue
                    for c in content:
                        if not isinstance(c, dict) or c.get('type') != 'tool_use': continue
                        name = c.get('name','')
                        blob = json.dumps(c.get('input', {}))
                        for m in PATH_RE.findall(blob):
                            p = normalize(proj_of(m))
                            if p: events.append((name, p))
        except OSError:
            continue

        if home is None or not events: continue
        sessions += 1
        outside = [(n,p) for n,p in events if p != home]
        if not outside: continue
        xproj_sessions += 1
        per_session_refs.append(len(outside))
        for n,p in outside:
            target_hits[p] += 1
            pair_hits[(home,p)] += 1
        # flail metric: search-tool calls against outside projects before the first outside Read
        pre = 0
        for n,p in events:
            if p == home: continue
            if n in READ_TOOLS: break
            if n in SEARCH_TOOLS or n == 'Bash': pre += 1
        flail_costs.append(pre)

print(f"window: last {DAYS} days")
print(f"sessions with tool activity in ~/Workspace/dev: {sessions}")
print(f"sessions that referenced ANOTHER project: {xproj_sessions} ({100*xproj_sessions/max(sessions,1):.1f}%)")
if per_session_refs:
    per_session_refs.sort()
    n = len(per_session_refs)
    print(f"cross-project path touches per such session: median {per_session_refs[n//2]}, p90 {per_session_refs[int(n*0.9)]}, max {per_session_refs[-1]}")
if flail_costs:
    flail_costs.sort()
    n = len(flail_costs)
    print(f"search calls before first cross-project Read: median {flail_costs[n//2]}, p90 {flail_costs[int(n*0.9)]}, max {flail_costs[-1]}")
    print(f"  sessions needing 0 searches (went straight to the file): {sum(1 for x in flail_costs if x==0)}/{n}")
    print(f"  sessions needing 5+ searches: {sum(1 for x in flail_costs if x>=5)}/{n}")
print("\ntop referenced-FROM-elsewhere projects:")
for p,c in target_hits.most_common(12): print(f"  {c:5d}  {p}")
print("\ntop (home -> referenced) pairs:")
for (h,t),c in pair_hits.most_common(12): print(f"  {c:5d}  {h}  ->  {t}")
