"""Human check of the need space: for each triplet in triplets.csv, is B or C closer in need to A? -> judgments.csv

A local page shows one triplet at a time, without the distances. Keys: 1 or left arrow
picks B, 2 or right arrow picks C, 0 or space means can't tell, u undoes the last answer.
Each answer is appended to judgments.csv (id, choice, time) as it is given, so the page
can be closed and reopened at any point.

    python judge.py    # then open http://localhost:8765
"""
import csv
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "judgments.csv"
TRIPLETS = list(csv.DictReader(open(HERE / "triplets.csv")))

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Need triplets</title>
<style>
:root { --bg: #f7f7f5; --card: #fff; --ink: #1d1d1b; --muted: #6b6b66; --line: #d9d9d4; --accent: #2f5d8a; }
@media (prefers-color-scheme: dark) { :root { --bg: #18181a; --card: #222226; --ink: #ececea; --muted: #9a9a96; --line: #3a3a3f; --accent: #8db4dc; } }
body { margin: 0; background: var(--bg); color: var(--ink); font: 17px/1.45 system-ui, sans-serif; }
main { max-width: 760px; margin: 40px auto; padding: 0 16px; }
.help { color: var(--muted); font-size: 14px; margin-bottom: 24px; }
.anchor { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 20px; font-size: 20px; font-weight: 600; text-align: center; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 18px 0; }
button { font: inherit; color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 18px; cursor: pointer; }
button:hover { border-color: var(--accent); }
.pick small { display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.row { display: flex; justify-content: space-between; align-items: center; color: var(--muted); font-size: 14px; }
.row button { padding: 8px 14px; font-size: 14px; }
@media (max-width: 560px) { .pair { grid-template-columns: 1fr; } }
</style></head><body><main>
<p class="help">Which need is closer to the one at the top? Judge the job to be done, not the wording:
would a solution to one go a long way toward the other? Keys: 1 / &larr; for left, 2 / &rarr; for right,
0 / space for can't tell, u to undo.</p>
<div id="app"></div>
</main><script>
let cur = null;
async function load() {
  cur = await (await fetch("next")).json();
  const app = document.getElementById("app");
  if (cur.finished) { app.innerHTML = `<p class="anchor">All ${cur.total} done. Thank you.</p>`; return; }
  app.innerHTML = `<div class="anchor"></div>
    <div class="pair"><button class="pick" data-c="b"><small>1</small><span></span></button>
    <button class="pick" data-c="c"><small>2</small><span></span></button></div>
    <div class="row"><span>${cur.done} of ${cur.total}</span>
    <span><button data-c="none">can't tell</button> <button id="undo">undo</button></span></div>`;
  app.querySelector(".anchor").textContent = cur.a;
  const spans = app.querySelectorAll(".pick span");
  spans[0].textContent = cur.b; spans[1].textContent = cur.c;
  app.querySelectorAll("[data-c]").forEach(b => b.onclick = () => answer(b.dataset.c));
  document.getElementById("undo").onclick = undo;
}
async function answer(choice) {
  if (!cur || cur.finished) return;
  await fetch("answer", {method: "POST", body: JSON.stringify({id: cur.id, choice})});
  load();
}
async function undo() { await fetch("undo", {method: "POST"}); load(); }
document.addEventListener("keydown", e => {
  const k = {"1": "b", "ArrowLeft": "b", "2": "c", "ArrowRight": "c", "0": "none", " ": "none"}[e.key];
  if (k) { e.preventDefault(); answer(k); } else if (e.key === "u") undo();
});
load();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
	def send(self, body, kind):
		self.send_response(200)
		self.send_header("Content-Type", kind)
		self.end_headers()
		self.wfile.write(body.encode())

	def do_GET(self):
		if self.path != "/next":
			return self.send(PAGE, "text/html; charset=utf-8")
		done = {r["id"] for r in csv.DictReader(open(OUT))} if OUT.exists() else set()
		todo = [t for t in TRIPLETS if t["id"] not in done]
		state = {"done": len(TRIPLETS) - len(todo), "total": len(TRIPLETS), "finished": not todo}
		if todo:
			state |= {k: todo[0][k] for k in ("id", "a", "b", "c")}
		self.send(json.dumps(state), "application/json")

	def do_POST(self):
		body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
		lines = open(OUT).read().splitlines(keepends=True) if OUT.exists() else ["id,choice,time\n"]
		if self.path == "/answer":
			r = json.loads(body)
			lines.append(f"{r['id']},{r['choice']},{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
		elif self.path == "/undo" and len(lines) > 1:
			lines.pop()
		open(OUT, "w").writelines(lines)
		self.send("{}", "application/json")

	def log_message(self, *args):
		pass


print("http://localhost:8765  (Ctrl-C to stop)")
HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
