/* 保留期限复核页面交互（与后端规则、复核服务、迁移分开维护）。 */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function flash(message, ok = false) {
  const el = $("flash");
  if (!message) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="flash ${ok ? "info" : "err"}">${esc(message)}</div>`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", "X-User-Id": $("user").value, ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.error?.message || `请求失败 (${res.status})`;
    const err = new Error(msg);
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

function tag(text, cls) {
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

function reviewRow(r) {
  const typeText = r.request_type === "extend" ? "延期" : "提前结束";
  let statusTag;
  if (r.status === "pending") statusTag = tag("待处理", "pending");
  else if (r.status === "approved") statusTag = tag("已批准", "approved");
  else statusTag = tag("已拒绝", "rejected");
  const note = r.status === "rejected"
    ? `<div class="reject-reason">拒绝原因：${esc(r.decision_note)}（处理人 ${esc(r.decided_by)} @ ${esc(r.decided_at)}）</div>`
    : (r.status === "approved" && r.decision_note
        ? `<div class="muted">处理说明：${esc(r.decision_note)}（${esc(r.decided_by)} @ ${esc(r.decided_at)}）</div>` : "");
  return `<tr>
      <td>#${r.id} ${typeText}${statusTag}</td>
      <td>${esc(r.current_retention_until)} → <b>${esc(r.requested_retention_until)}</b></td>
      <td>${esc(r.submitted_by)}<br><span class="muted">${esc(r.submitted_at)}</span></td>
      <td>${esc(r.reason)}${note}</td>
    </tr>`;
}

function evidenceCard(e, caseId) {
  const hold = e.legal_hold ? tag("法律保留中", "hold") : "";
  const original = e.original_retention_until && e.original_retention_until !== e.retention_until
    ? `<span class="muted">（原日期 ${esc(e.original_retention_until)}）</span>` : "";
  const reviews = e.retention_reviews || [];
  const reviewTable = reviews.length
    ? `<table><thead><tr><th>申请</th><th>到期日变化</th><th>提交</th><th>原因 / 处理结果</th></tr></thead>
       <tbody>${reviews.map(reviewRow).join("")}</tbody></table>`
    : `<div class="muted">暂无期限复核记录</div>`;
  const chainTypes = (e.events || []).map((x) => x.event_type).join(" → ");
  return `<div class="card">
    <div><b>#${e.id} ${esc(e.label)}</b> <span class="muted">${esc(e.filename)}</span>
      ${tag(e.status, e.status === "released" ? "rejected" : "ok")}${hold}</div>
    <div class="muted">保管人：${esc(e.current_custodian)} ｜ SHA-256：<span class="chain">${esc(e.sha256.slice(0, 16))}…</span>
      ${e.integrity_valid === false ? tag("哈希异常", "rejected") : ""}</div>
    <div>保留到期日：<b>${esc(e.retention_until)}</b> ${original}</div>
    <h2>提交期限复核（保管员）</h2>
    <div>
      <select id="type-${e.id}"><option value="extend">延期</option><option value="early_end">提前结束</option></select>
      <input id="date-${e.id}" type="date">
      <input id="reason-${e.id}" type="text" placeholder="原因（至少 5 字）" size="28">
      <button onclick='submitReview(${e.id})'>提交申请</button>
    </div>
    <h2>处理复核（案件创建人 / 审计员）</h2>
    ${reviewTable}
    <h2>事件链</h2>
    <div class="chain muted">${esc(chainTypes) || "（无）"}</div>
  </div>`;
}

async function loadReport() {
  flash("");
  const caseId = Number($("case").value);
  try {
    const report = await api(`/api/cases/${caseId}/report`);
    const root = $("content");
    root.style.display = "block";
    root.innerHTML = `<h2>案件 ${esc(report.case.case_number)}：${esc(report.case.title)}</h2>
      <div>完整性：${report.overall_integrity_valid ? tag("全部通过", "ok") : tag("存在异常", "rejected")}
        ｜证据数：${report.evidence_count}</div>
      ${report.evidence.map((e) => evidenceCard(e, caseId)).join("")}`;
  } catch (err) {
    $("content").style.display = "none";
    flash(err.message);
  }
}

async function submitReview(evidenceId) {
  flash("");
  const body = {
    request_type: $(`type-${evidenceId}`).value,
    new_retention_until: $(`date-${evidenceId}`).value,
    reason: $(`reason-${evidenceId}`).value,
  };
  try {
    await api(`/api/evidence/${evidenceId}/retention-reviews`, { method: "POST", body: JSON.stringify(body) });
    flash("期限复核申请已提交", true);
    await loadReport();
  } catch (err) { flash(err.message); }
}

async function decideReview(reviewId, approve) {
  flash("");
  const note = $(`note-${reviewId}`)?.value || "";
  if (!approve && !note.trim()) { flash("拒绝时必须填写原因"); return; }
  try {
    await api(`/api/retention-reviews/${reviewId}/decision`,
      { method: "POST", body: JSON.stringify({ approve, decision_note: note }) });
    flash(approve ? "已批准：到期日已更新，事件链已追加" : "已拒绝，拒绝原因已记录", true);
    await loadPending();
  } catch (err) { flash(err.message); }
}

function pendingCard(r) {
  const typeText = r.request_type === "extend" ? "延期" : "提前结束";
  return `<div class="card">
    <div><b>申请 #${r.id}</b>（证据 #${r.evidence_id}）${tag(typeText, "pending")} ${tag("待处理", "pending")}</div>
    <div>到期日：${esc(r.current_retention_until)} → <b>${esc(r.requested_retention_until)}</b></div>
    <div>提交人：${esc(r.submitted_by)} @ ${esc(r.submitted_at)}</div>
    <div>原因：${esc(r.reason)}</div>
    <div class="muted">提交时：法律保留=${r.legal_hold_at_submit ? "是" : "否"}，证据状态=${esc(r.evidence_status_at_submit)}，提交人角色=${esc(r.submitter_role_at_submit)}</div>
    <input id="note-${r.id}" type="text" placeholder="处理说明（拒绝必填）" size="34">
    <button class="primary" onclick='decideReview(${r.id}, true)'>批准</button>
    <button class="danger" onclick='decideReview(${r.id}, false)'>拒绝</button>
  </div>`;
}

async function loadPending() {
  flash("");
  const caseId = Number($("case").value);
  try {
    const list = await api(`/api/cases/${caseId}/retention-reviews?status=pending`);
    const root = $("content");
    root.style.display = "block";
    root.innerHTML = `<h2>待处理的期限复核申请（案件 #${caseId}）</h2>` +
      (list.length ? list.map(pendingCard).join("") : '<div class="muted">没有待处理申请。</div>') +
      '<button onclick="loadReport()">返回案件报告</button>';
  } catch (err) {
    $("content").style.display = "none";
    flash(err.message);
  }
}
