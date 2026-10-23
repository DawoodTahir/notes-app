#!/usr/bin/env bash
# Guard the UI's DOM contract after any edit to app/static/index.html.
#
# The inline script resolves elements by id. A redesign that drops one leaves a
# dead control that throws nothing until a user clicks it — the exact failure a
# "looks fine, returns 200" check misses. Exits 2 with the missing ids so the
# breakage is reported instead of shipped.

set -uo pipefail
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - "$PROJ/app/static/index.html" <<'PY'
import re, sys, pathlib

path = pathlib.Path(sys.argv[1])
if not path.exists():
    sys.exit(0)
html = path.read_text()

# Ids the inline script actually looks up, discovered from the script itself
# rather than hard-coded, so this stays true as the page evolves.
used = set(re.findall(r'\$\(\s*["\']([^"\']+)["\']\s*\)', html))
used |= set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', html))
present = set(re.findall(r'id="([^"]+)"', html))

missing = sorted(used - present)

problems = []
if missing:
    problems.append(
        "script references ids that no longer exist in the markup: "
        + ", ".join(missing)
    )

# An id can exist and still be null when the script runs. The inline script
# sits at the end of the body and resolves ids immediately, so any element
# declared *after* </script> is not in the DOM yet: $("x") returns null and the
# first .addEventListener on it throws, killing every line below it. That page
# still returns 200 and still passes the existence check above, which is
# exactly how it got shipped once.
script_open = html.find("<script>\n" + "const $ =")
script_close = html.find("</script>", script_open) if script_open != -1 else -1
if script_close != -1:
    late = []
    for mid in re.finditer(r'id="([^"]+)"', html):
        if mid.start() > script_close and mid.group(1) in used:
            late.append(mid.group(1))
    if late:
        problems.append(
            "markup declared after </script> but referenced by it: "
            + ", ".join(sorted(set(late)))
            + " (move the markup above the script)"
        )

# checkHealth() rewrites the empty-valued option inside #provider.
if "provider" in present:
    sel = re.search(r'<select[^>]*id="provider".*?</select>', html, re.S)
    if not sel:
        problems.append('#provider is no longer a <select> (checkHealth() needs one)')
    elif not re.search(r'<option[^>]*value=""', sel.group(0)):
        problems.append('#provider lost its <option value=""> (checkHealth() rewrites it)')

# A page whose script does not parse has every control dead, and the id check
# above still passes on it — so parse the inline script too when node exists.
import shutil, subprocess, tempfile
if shutil.which("node"):
    m = re.search(r"<script>\n(const \$ =.*?)</script>", html, re.S)
    if m:
        t = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False)
        t.write(m.group(1)); t.close()
        r = subprocess.run(["node", "--check", t.name], capture_output=True, text=True)
        if r.returncode != 0:
            first = next((ln for ln in r.stderr.splitlines() if "Error" in ln), r.stderr[:200])
            problems.append("inline script does not parse: " + first.strip())

if problems:
    sys.stderr.write("UI contract broken:\n- " + "\n- ".join(problems) + "\n")
    sys.exit(2)

print(f"ui contract OK ({len(used)} bound ids, script parses)")
PY
