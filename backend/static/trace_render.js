// Trace 渲染：离线页面（eval/trace_web.py）与调试台（/console）共用，改一处两边生效。
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pretty = v => esc(JSON.stringify(v, null, 2));
const find = (t, node, ev) => t.events.find(e => e.node === node && e.event === ev);
const all = (t, node, ev) => t.events.filter(e => e.node === node && e.event === ev);

function meta(t) {
  const first = t.events[0] || {};
  const req = t.events.find(e => e.event === 'request' || e.event === 'agent_request' || e.event === 'routine_request');
  const text = (t.events.find(e => e.payload && typeof e.payload.text === 'string' && (e.event === 'request' || e.event === 'input_received')) || {}).payload?.text
    || (t.events.find(e => e.payload && e.payload.input) || {}).payload?.input || '';
  const rr = find(t, 'routine', 'routine_request');
  const ar = find(t, 'assistant', 'assistant_request');
  const ev = find(t, 'eval', 'evaluation');
  let chain = first.node || '-';
  if (ar) chain = '智能助手';
  else if (find(t, 'plan_v2', 'request')) chain = '训练计划 v2';
  else if (rr) chain = '训练安排';
  else if (t.events.some(e => e.node === 'generator')) chain = '训练计划';
  else if (t.events.some(e => e.node === 'agent')) chain = '工具调用';
  else if (t.events.some(e => e.node === 'executor')) chain = '查询';
  else if (t.events.some(e => e.node === 'extractor')) chain = '记录';
  return { text: typeof text === 'string' ? text : JSON.stringify(text), caseId: (ar || rr)?.payload.case_id, verdict: ev?.payload.verdict, chain, ts: first.ts };
}

function localTime(ts) {
  if (!ts) return '';
  const d = new Date(ts), pad = n => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function verdictBadge(v) {
  if (!v) return '<span class="badge grey">未评测</span>';
  const cls = v === 'PASS' ? 'ok' : v === 'REVIEW' ? 'warn' : 'bad';
  return `<span class="badge ${cls}">${v}</span>`;
}

function unitsHtml(units, opts = {}) {
  if (!units || !units.length) return '<span class="legend">（空）</span>';
  return '<div class="order">' + units.map((u, i) => {
    const cls = ['unit'];
    if (u.status === 'paused') cls.push('paused');
    if (u.missing) cls.push('missing');
    if (opts.moved && u.unit_id === opts.moved) cls.push('moved');
    const label = esc(u.name || u.unit_id) + (u.status === 'paused' ? '（暂停）' : '');
    return (i ? '<span class="arrow">→</span>' : '') + `<span class="${cls.join(' ')}" title="${esc(u.unit_id)}">${label}</span>`;
  }).join('') + '</div>';
}

function argSummary(args) {
  if (!args || typeof args !== 'object') return esc(args);
  const parts = Object.entries(args).map(([k, v]) => Array.isArray(v) ? `${k}=[${v.length} 项] ${v.join(', ')}` : `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`);
  return esc(parts.join(' · ') || '（无参数）');
}

function inputSummary(m) {
  if (m.role === 'user') return `用户：${esc(m.content)}`;
  const c = m.content || {};
  const brief = c.ok === false ? `<span class="badge bad">${esc(c.error_code)}</span>${esc(c.message || '')}`
    : outSummary({ ok: true, output: c });
  return `看到 ${esc(m.tool || '工具')} 结果：${brief}`;
}

function outSummary(a) {
  const o = a.output || {};
  if (!a.ok) return `<span class="badge bad">${esc(a.error_code)}</span>${esc(a.message || '')}`;
  if (o.units) return `返回 ${o.units.length} 个单元：${esc(o.units.map(u => u.name).join('、'))}`;
  if (o.written === true) return '已写入';
  if (typeof o.written === 'number') return `已写入 ${o.written} 条（${esc(o.state)}）` + (o.idempotent_replay ? '，幂等重放' : '');
  if (o.week) return `${esc(o.week.label)} ${esc(o.week.start)}～${esc(o.week.end)}：练 ${o.training_days} 天、${o.record_count} 条，力量 ${o.strength_days} 天，记时长 ${o.timed_minutes} 分钟` + (o.unrecognized_exercises.length ? `，未识别 ${esc(o.unrecognized_exercises.join('、'))}` : '');
  if (o.match === 'single') return `找到 ${esc(o.action.name)}：${esc(o.action.cue)}`;
  if (o.match === 'multiple') return `匹配到多个：${esc(o.candidates.join('、'))}`;
  if (o.match === 'none') return esc(o.message);
  if (Array.isArray(o.segments)) {
    const n = o.segments.reduce((k, s) => k + s.items.length, 0);
    return `计划 ${o.segments.length} 段 ${n} 个动作` + (o.computed_duration_min != null ? `，计算时长 ${o.computed_duration_min} 分钟` : '');
  }
  if (Array.isArray(o.plan)) return `计划 ${o.plan.length} 个动作：${esc(o.plan.map(x => x.name).join('、'))}`;
  if (Array.isArray(o.records)) return `查到 ${o.count} 条`;
  if (typeof o.count === 'number') return `次数 ${o.count}`;
  return esc(JSON.stringify(o));
}

const SEGMENT_LABEL = { warmup: '热身', main: '训练', cooldown: '拉伸' };

function itemDose(i) {
  const dose = i.reps != null ? `${i.reps} 次` : `${i.seconds ?? '-'} 秒`;
  return (i.sets != null ? `${i.sets} 组 × ` : '') + dose + (i.unilateral ? '（每侧）' : '') + (i.rest_sec ? `，休息 ${i.rest_sec} 秒` : '');
}

function segmentsHtml(segments) {
  return segments.map(s => `<h4 style="margin:10px 0 4px">${esc(s.label || SEGMENT_LABEL[s.segment] || s.segment)}${s.format === 'circuit' ? `（循环 ${s.rounds} 轮，轮间休息 ${s.rest_between_rounds ?? 0} 秒）` : ''}</h4>` +
    `<ul class="plist">${s.items.map(i => `<li>${esc(i.name || i.id)}　${esc(itemDose(i))}${i.id == null ? ' <span class="badge bad">库中没有</span>' : ''}${i.cue ? `<br><span class="legend">${esc(i.cue)}</span>` : ''}</li>`).join('')}</ul>`).join('');
}

// V10 计划 v2 子链路：需求解析 → 候选 → 每次编排的校验结果 → 计划
function planV2Card(t) {
  const req = find(t, 'plan_v2', 'request');
  if (!req) return '';
  const need = find(t, 'plan_v2', 'need_parsed')?.payload;
  const cand = find(t, 'plan_v2', 'candidates')?.payload;
  const checks = all(t, 'plan_v2', 'compose_checked').map(e => e.payload);
  const res = find(t, 'plan_v2', 'result')?.payload || {};
  const out = all(t, 'tool', 'tool_attempt').map(e => e.payload).find(a => a.tool === 'generate_workout_plan')?.output
    || t.planOutput || {};
  const rows = [
    `<span class="k">引擎</span><span>${esc(req.payload.engine)}</span>`,
    `<span class="k">收到的需求</span><span>${esc(req.payload.text)}</span>`,
  ];
  if (need) rows.push(`<span class="k">解析结果</span><span>${need.ok ? esc(Object.entries(need.need).filter(([k]) => k !== 'defaults').map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(' · ')) : `<span class="badge bad">${esc(need.error)}</span>`}</span>`);
  if (cand) rows.push(`<span class="k">候选动作</span><span>${cand.count} 个（可用器械：${esc(cand.available_equipment.join('、') || '徒手')}）</span>`);
  checks.forEach(c => rows.push(`<span class="k">第 ${c.attempt} 次编排</span><span>${c.violations.length
    ? c.violations.map(v => `<span class="badge bad">${esc(v.rule)}</span>${esc(v.detail)}`).join('<br>')
    : `<span class="badge ok">全部通过</span> 计算时长 ${c.computed_min} 分钟`}</span>`));
  rows.push(`<span class="k">结果</span><span>${res.ok ? '<span class="badge ok">交付</span>' : `<span class="badge bad">${esc(res.error_code)}</span>${esc(res.message || '')}`}</span>`);
  const plan = out.segments ? `<div style="margin-top:12px"><b>计划</b>（目标 ${esc(out.target_duration_min ?? out.claimed_duration_min ?? '-')} 分钟${out.computed_duration_min != null ? `，代码计算 ${out.computed_duration_min} 分钟` : ''}）${segmentsHtml(out.segments)}${out.note ? `<p class="legend">${esc(out.note)}</p>` : ''}</div>` : '';
  return `<div class="card"><h2>训练计划 v2 子链路</h2><div class="body"><div class="kv">${rows.join('')}</div>${plan}</div></div>`;
}

// 工具循环视图：训练安排链路（V8）与统一助手（V9）共用
function loopView(t) {
  const node = find(t, 'assistant', 'assistant_request') ? 'assistant' : 'routine';
  const before = find(t, 'state', 'state_before')?.payload.units || [];
  const after = find(t, 'state', 'state_after')?.payload.units || [];
  const req = find(t, node, `${node}_request`)?.payload || {};
  const res = find(t, node, 'result')?.payload || {};
  const userText = find(t, node, 'request')?.payload.text || '';
  const ev = find(t, 'eval', 'evaluation')?.payload;
  const rounds = all(t, 'llm', 'llm_round');
  const attempts = all(t, 'tool', 'tool_attempt').map(e => e.payload);
  const writes = attempts.filter(a => a.tool === 'set_routine_order');
  const recordWrites = attempts.filter(a => a.tool === 'create_record');
  const plans = attempts.filter(a => a.tool === 'generate_workout_plan');
  // 含训练计划子链路（planner / generator）的消耗
  const tokens = t.events.reduce((s, e) => s + (e.payload?.usage?.total_tokens || 0), 0);
  const llmMs = rounds.reduce((s, e) => s + (e.payload.duration_ms || 0), 0);
  const toolMs = attempts.reduce((s, a) => s + (a.duration_ms || 0), 0);
  const masked = writes.length > 1 && !writes[0].ok && writes.some(w => w.ok);
  const moved = before.length && after.length ? after.find((u, i) => {
    const rest = after.filter(x => x.unit_id !== u.unit_id).map(x => x.unit_id);
    return JSON.stringify(rest) === JSON.stringify(before.filter(x => x.unit_id !== u.unit_id).map(x => x.unit_id))
      && JSON.stringify(after.map(x => x.unit_id)) !== JSON.stringify(before.map(x => x.unit_id));
  })?.unit_id : null;

  // 结论：用大白话说这条链路发生了什么
  const lines = [];
  lines.push(`模型一共调用 ${rounds.length} 轮，工具调用 ${attempts.length} 次，其中写入 ${writes.length + recordWrites.length} 次、成功 ${res.writes ?? 0} 次。`);
  if (!attempts.length) lines.push('没有调用工具，直接回复。');
  recordWrites.forEach(w => lines.push(`第 ${w.round} 轮记录训练：${esc(argSummary(w.args))}，` + (w.ok ? '已写入' : `失败（${esc(w.error_code)}）`) + '。'));
  attempts.filter(a => a.tool === 'query_records' || a.tool === 'count_exercise').forEach(a =>
    lines.push(`第 ${a.round} 轮${a.tool === 'query_records' ? '查询' : '统计'}：${esc(argSummary(a.args))} → ${a.ok ? outSummary(a) : '失败'}。`));
  plans.forEach(p => lines.push(`第 ${p.round} 轮生成训练计划：` + (p.ok ? outSummary(p) : `未交付（${esc(p.error_code)}：${esc(p.message || '')}）`) + '。'));
  if (!writes.length && attempts.some(a => a.tool === 'get_routine')) lines.push('查看了训练安排，未调整。');
  writes.forEach(w => {
    const order = w.args?.order || [];
    const missing = before.filter(u => !order.includes(u.unit_id));
    lines.push(`第 ${w.round} 轮第 ${w.attempt} 次写入：提交 ${order.length} 项，` + (w.ok ? '通过' : `被拒（${w.error_code}）`) +
      (missing.length ? `，缺 ${missing.map(u => u.name + (u.status === 'paused' ? '（暂停）' : '')).join('、')}` : '') + '。');
  });
  if (masked) lines.push('<b>首次写入被拒、重试后成功：只看最终结果会以为一切正常，这次失败被掩盖了。</b>');
  if (res.error_code) lines.push(`<b>链路异常结束：${esc(res.error_code)}</b>`);

  // 执行步骤表：模型轮次与工具调用按时间穿插
  const toolTotals = {};
  attempts.forEach(a => toolTotals[a.tool] = (toolTotals[a.tool] || 0) + 1);
  let seq = 0;
  const rows = [];
  t.events.forEach(e => {
    if (e.node === 'llm' && e.event === 'llm_round') {
      const p = e.payload;
      const decided = p.tool_calls.length ? '决定调用：' + p.tool_calls.map(c => c.name).join('、') : '最终回复：' + (p.text || '（空）');
      const out = (p.reason ? `<span class="reason">理由：${esc(p.reason)}</span><br>` : '') + esc(decided);
      // 本轮新看到的输入（V9 起记录）；旧 Trace 没有该字段时退回消息条数
      const input = p.new_inputs ? p.new_inputs.map(inputSummary).join('<br>') : `带上前面 ${p.message_count} 条消息`;
      rows.push({ seq: ++seq, kind: '模型', name: `第 ${p.round} 轮决策`, input, output: out,
        ms: p.duration_ms, ok: true, extra: `${p.usage?.total_tokens ?? '-'} token · 共 ${p.message_count} 条消息`,
        detail: { input: p.new_inputs || { round: p.round, message_count: p.message_count }, output: { reason: p.reason, tool_calls: p.tool_calls, text: p.text, usage: p.usage } } });
    } else if (e.node === 'tool' && e.event === 'tool_attempt') {
      const a = e.payload;
      let orderHtml = '';
      if (a.tool === 'set_routine_order') {
        const order = a.args?.order || [];
        const byId = Object.fromEntries(before.map(u => [u.unit_id, u]));
        const submitted = order.map(id => byId[id] || { unit_id: id, name: id + '（未知）' });
        const missing = before.filter(u => !order.includes(u.unit_id)).map(u => ({ ...u, missing: true }));
        orderHtml = `<h4>提交的顺序（红色划线为缺失项）</h4>${unitsHtml(submitted.concat(missing))}`;
      }
      rows.push({ seq: ++seq, kind: '工具', name: `${a.tool}  ${a.attempt}/${toolTotals[a.tool]}`, input: (a.reason ? `<span class="reason">理由：${esc(a.reason)}</span><br>` : '') + argSummary(a.args), output: outSummary(a),
        ms: a.duration_ms, ok: a.ok, extra: `第 ${a.round} 轮`, detail: { input: a.args, output: a.output }, orderHtml });
    }
  });

  const flow = [
    '<span class="step">用户输入</span>',
    ...t.events.filter(e => (e.node === 'llm' && e.event === 'llm_round') || (e.node === 'tool' && e.event === 'tool_attempt')).map(e =>
      e.node === 'llm' ? `<span class="step">模型 第${e.payload.round}轮</span>`
        : `<span class="step ${e.payload.ok ? 'ok' : 'bad'}">${esc(e.payload.tool)} ${e.payload.ok ? '✓' : '✗'}</span>`),
    `<span class="step ${res.ok ? 'ok' : 'bad'}">结束</span>`,
  ].join('<span class="arrow">→</span>');

  return `
  <div class="card"><h2><span>${esc(userText)}</span>${verdictBadge(ev?.verdict)}</h2>
    <div class="body kv">
      <span class="k">trace_id</span><span>${esc(t.id)}</span>
      <span class="k">Case</span><span>${req.case_id ? `${esc(req.case_id)} · Fixture ${esc(req.fixture || '-')} · ${esc(req.arm || '-')}` : (req.arm === 'console' ? '调试台输入（非评测 Case）' : '-')}</span>
      <span class="k">模型 / Prompt</span><span>${esc(req.model)} · ${esc(req.prompt_name)} ${esc(req.prompt_version)}</span>
      <span class="k">最终回复</span><span>${esc(res.text || '（无）')}</span>
      ${ev && ev.failures.length ? `<span class="k">未通过断言</span><span>${ev.failures.map(f => `<span class="badge bad">${esc(f.check)}</span>`).join(' ')}</span>` : ''}
    </div></div>
  <div class="card"><h2>结论</h2><div class="body"><div class="conclusion ${masked || res.error_code ? 'alert' : ''}">${lines.map(l => `<p>${l}</p>`).join('')}</div>
    <div class="flow" style="margin-top:12px">${flow}</div></div></div>
  ${node === 'routine' || attempts.some(a => a.tool === 'get_routine' || a.tool === 'set_routine_order') ? `<div class="card"><h2>训练安排：操作前后</h2><div class="body">
    <h4 style="margin:0 0 6px;color:var(--muted);font-weight:500;font-size:12px">操作前（灰色虚线为暂停单元，用户默认看不到）</h4>${unitsHtml(before)}
    <h4 style="margin:14px 0 6px;color:var(--muted);font-weight:500;font-size:12px">操作后（黄框为被移动的单元）</h4>${unitsHtml(after, { moved })}
  </div></div>` : ''}
  ${recordWrites.some(w => w.ok) ? `<div class="card"><h2>本次写入的训练记录</h2><div class="body"><ul class="plist">${recordWrites.filter(w => w.ok).map(w =>
    `<li>${esc(argSummary(w.args))}<span class="legend">　${esc((w.output.ids || []).join(', '))}</span></li>`).join('')}</ul></div></div>` : ''}
  ${planV2Card(t)}
  ${plans.filter(p => !p.output?.engine).map(p => `<div class="card"><h2>训练计划子链路</h2><div class="body kv">
    <span class="k">用户原话</span><span>${esc(userText)}</span>
    <span class="k">助手转述</span><span>${esc(p.args?.request || '')}</span>
    <span class="k">结果</span><span>${p.ok ? p.output.plan.map(x => esc(`${x.name} ${x.sets}组×${x.reps}次 休息${x.rest_sec}秒`)).join('<br>') + (p.output.note ? `<br><span class="legend">${esc(p.output.note)}</span>` : '') : `<span class="badge bad">${esc(p.error_code)}</span>`}</span>
    </div></div>`).join('')}
  <div class="card"><h2>执行步骤与耗时<span class="chips"><span class="chip">模型合计 ${(llmMs / 1000).toFixed(3)} s</span><span class="chip">工具合计 ${(toolMs / 1000).toFixed(3)} s</span><span class="chip">Token ${tokens}</span></span></h2>
    ${stepsTable(rows)}</div>`;
}

function stepsTable(rows) {
  return `<table><thead><tr><th style="width:44px"></th><th style="width:50px">序号</th><th style="width:70px">类型</th><th style="width:190px">名称</th><th>输入</th><th>输出</th><th style="width:90px">耗时</th><th style="width:80px">结果</th></tr></thead><tbody>
  ${rows.map((r, i) => `
    <tr class="row ${r.ok ? '' : 'failed'}"><td><button class="toggle" data-r="${i}">+</button></td><td>${r.seq}</td>
      <td><span class="badge ${r.kind === '模型' ? 'grey' : 'info'}">${r.kind}</span></td><td>${esc(r.name)}<div class="legend">${esc(r.extra || '')}</div></td>
      <td class="sum"><span class="badge info">JSON</span>${r.input}</td><td class="sum"><span class="badge grey">${r.kind === '模型' ? '文本' : 'JSON'}</span>${r.output}</td>
      <td>${r.ms != null ? (r.ms / 1000).toFixed(3) + ' s' : '-'}</td>
      <td><span class="badge ${r.ok ? 'ok' : 'bad'}">${r.ok ? '成功' : '失败'}</span></td></tr>
    <tr class="detail-row" data-d="${i}" hidden><td></td><td colspan="7">
      ${r.orderHtml ? `<div style="margin-bottom:10px">${r.orderHtml}</div>` : ''}
      <div class="detail"><div><h4>完整输入</h4><pre>${pretty(r.detail.input)}</pre></div><div><h4>完整输出</h4><pre>${pretty(r.detail.output)}</pre></div></div>
    </td></tr>`).join('')}
  </tbody></table>`;
}

function genericView(t) {
  const m = meta(t);
  const rows = t.events.map((e, i) => {
    const p = e.payload || {};
    const failed = p.ok === false;
    const summary = Object.entries(p).slice(0, 4).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' · ');
    return { seq: i + 1, kind: e.node, name: e.event, input: esc(summary.slice(0, 200)), output: '', ms: p.duration_ms, ok: !failed, extra: (e.ts || '').slice(11, 23), detail: { input: e, output: p } };
  });
  return `<div class="card"><h2><span>${esc(m.text || t.id)}</span>${verdictBadge(m.verdict)}</h2>
    <div class="body kv"><span class="k">trace_id</span><span>${esc(t.id)}</span><span class="k">链路</span><span>${esc(m.chain)}</span></div></div>
    <div class="card"><h2>事件</h2>${stepsTable(rows)}</div>`;
}

// 把一条链路渲染进容器，并绑定 ＋ 展开
function planV2View(t) {
  const ev = find(t, 'eval', 'evaluation')?.payload;
  const tokens = t.events.reduce((s, e) => s + (e.payload?.usage?.total_tokens || 0), 0);
  // 评测 Runner 直接调用引擎，没有工具调用事件；计划从 eval 事件里取
  t.planOutput = ev?.plan_output || {};
  return `<div class="card"><h2><span>${esc(find(t, 'plan_v2', 'request').payload.text)}</span>${verdictBadge(ev?.verdict)}</h2>
    <div class="body kv"><span class="k">trace_id</span><span>${esc(t.id)}</span><span class="k">Token</span><span>${tokens}</span>
    ${ev && ev.failures?.length ? `<span class="k">未通过断言</span><span>${ev.failures.map(f => `<span class="badge bad">${esc(f.check)}</span>`).join(' ')}</span>` : ''}</div></div>
    ${planV2Card(t)}${genericView(t).split('<div class="card"><h2>事件</h2>')[1] ? '<div class="card"><h2>事件</h2>' + genericView(t).split('<div class="card"><h2>事件</h2>')[1] : ''}`;
}

function renderTrace(container, t) {
  const loop = find(t, 'routine', 'routine_request') || find(t, 'assistant', 'assistant_request');
  const standalonePlan = !loop && find(t, 'plan_v2', 'request');
  container.innerHTML = loop ? loopView(t) : standalonePlan ? planV2View(t) : genericView(t);
  container.querySelectorAll('.toggle').forEach(b => b.onclick = () => {
    const row = container.querySelector(`tr[data-d="${b.dataset.r}"]`);
    row.hidden = !row.hidden;
    b.textContent = row.hidden ? '+' : '−';
  });
}
