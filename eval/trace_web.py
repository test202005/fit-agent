"""把 Trace 生成一个本地网页：左侧选链路，右侧看执行步骤表，点 ＋ 展开完整输入输出。

用法：
    .venv/bin/python eval/trace_web.py              # 最近 300 条链路，生成后自动打开
    .venv/bin/python eval/trace_web.py --limit 50 --no-open
    .venv/bin/python eval/trace_web.py --trace-id <trace_id>

产物写到 eval/results/trace-view.html（已被 Git 忽略），单文件、离线可看，不上传任何数据。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.run_intent_eval import RESULTS_DIR, TRACE_PATH  # noqa: E402
from eval.trace_view import load_events  # noqa: E402


OUTPUT_PATH = RESULTS_DIR / "trace-view.html"


def group_traces(events: list[dict], limit: int, trace_id: str | None) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for event in events:
        grouped.setdefault(event["trace_id"], []).append(event)
    if trace_id:
        if trace_id not in grouped:
            raise SystemExit(f"trace not found: {trace_id}")
        return [{"id": trace_id, "events": grouped[trace_id]}]
    ids = list(grouped)[-limit:]
    return [{"id": tid, "events": grouped[tid]} for tid in reversed(ids)]


STATIC_DIR = ROOT / "backend" / "static"


def render_html(traces: list[dict]) -> str:
    data = json.dumps(traces, ensure_ascii=False).replace("</", "<\\/")
    css = (STATIC_DIR / "trace.css").read_text(encoding="utf-8")
    render = (STATIC_DIR / "trace_render.js").read_text(encoding="utf-8")
    return PAGE.replace("__CSS__", css).replace("__RENDER__", render).replace("__DATA__", data)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=300, help="最近多少条链路")
    parser.add_argument("--trace-id", help="只看这一条")
    parser.add_argument("--path", default=str(TRACE_PATH))
    parser.add_argument("--no-open", action="store_true", help="只生成，不自动打开浏览器")
    args = parser.parse_args()

    traces = group_traces(load_events(Path(args.path)), args.limit, args.trace_id)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(render_html(traces), encoding="utf-8")
    print(OUTPUT_PATH)
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(OUTPUT_PATH)], check=False)
    return 0


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>fit-agent Trace</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="layout">
  <aside>
    <header>
      <h1>fit-agent Trace</h1>
      <input id="q" placeholder="搜索输入、case_id 或 trace_id">
      <select id="chain" style="margin-top:8px;width:100%;padding:6px;border:1px solid var(--line);border-radius:6px"></select>
    </header>
    <div class="list" id="list"></div>
  </aside>
  <main id="main"><div class="empty">左侧选择一条链路</div></main>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
__RENDER__
</script>
<script>
const TRACES = JSON.parse(document.getElementById('data').textContent);
function renderList() {
  const q = document.getElementById('q').value.trim().toLowerCase();
  const chain = document.getElementById('chain').value;
  const html = TRACES.map((t, i) => {
    const m = meta(t);
    const hay = `${m.text} ${m.caseId || ''} ${t.id}`.toLowerCase();
    if ((q && !hay.includes(q)) || (chain && m.chain !== chain)) return '';
    return `<div class="item" data-i="${i}"><div class="t">${esc(m.text || t.id)}</div>
      <div class="m"><span class="badge info">${esc(m.chain)}</span>${m.caseId ? esc(m.caseId) : ''}${verdictBadge(m.verdict)}
      <span>${esc(localTime(m.ts))}</span></div></div>`;
  }).join('');
  document.getElementById('list').innerHTML = html || '<div class="empty">没有匹配</div>';
  document.querySelectorAll('.item').forEach(el => el.onclick = () => select(+el.dataset.i));
}

function select(i) {
  const t = TRACES[i];
  document.querySelectorAll('.item').forEach(el => el.classList.toggle('active', +el.dataset.i === i));
  const main = document.getElementById('main');
  renderTrace(main, t);
  main.scrollTop = 0;
}

const chains = [...new Set(TRACES.map(t => meta(t).chain))];
document.getElementById('chain').innerHTML = '<option value="">全部链路</option>' + chains.map(c => `<option${c === '训练安排' ? ' selected' : ''}>${esc(c)}</option>`).join('');
document.getElementById('q').oninput = renderList;
document.getElementById('chain').onchange = renderList;
renderList();
const firstRoutine = TRACES.findIndex(t => find(t, 'routine', 'routine_request') || find(t, 'assistant', 'assistant_request'));
if (TRACES.length) select(firstRoutine >= 0 ? firstRoutine : 0);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
