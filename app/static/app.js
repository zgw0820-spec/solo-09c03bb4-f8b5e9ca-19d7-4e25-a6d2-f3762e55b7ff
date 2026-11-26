"use strict";

const $ = (sel) => document.querySelector(sel);

const jsonInput = $("#json-input");
const runBtn = $("#run-btn");
const loadSampleBtn = $("#load-sample");
const clearBtn = $("#clear-btn");
const fileInput = $("#file-input");
const statusEl = $("#status");
const errorBox = $("#error-box");
const emptyState = $("#empty-state");
const resultsEl = $("#results");
const badge = $("#result-badge");

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function showErrors(errors) {
  errorBox.hidden = false;
  errorBox.innerHTML =
    "<strong>清单有 " +
    errors.length +
    " 处问题：</strong><ul>" +
    errors.map((e) => "<li>" + escapeHtml(e) + "</li>").join("") +
    "</ul>";
  resultsEl.hidden = true;
  emptyState.hidden = true;
  badge.textContent = "校验失败";
  badge.className = "badge warn";
}

function clearErrors() {
  errorBox.hidden = true;
  errorBox.innerHTML = "";
}

async function runPlan(sourceText) {
  clearErrors();
  emptyState.hidden = true;
  resultsEl.hidden = true;
  runBtn.disabled = true;
  setStatus("计算中…", "busy");

  try {
    const resp = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ json_text: sourceText }),
    });
    const data = await resp.json();

    if (!resp.ok) {
      const errors = Array.isArray(data.detail)
        ? data.detail
        : [data.detail || "请求失败"];
      showErrors(errors);
      setStatus("校验失败", "error");
      return;
    }
    render(data);
    const s = data.summary;
    setStatus(
      "完成：" + s.scheduled_count + "/" + s.pod_count + " 个 Pod 可启动"
    );
    badge.textContent =
      s.unscheduled_count === 0 ? "全部可启动" : s.unscheduled_count + " 个待复核";
    badge.className = "badge " + (s.unscheduled_count === 0 ? "ok" : "warn");
  } catch (err) {
    showErrors(["网络或服务错误：" + err.message]);
    setStatus("出错了", "error");
  } finally {
    runBtn.disabled = false;
  }
}

function stat(label, value, kind) {
  return (
    '<div class="stat ' +
    (kind || "") +
    '"><div class="k">' +
    escapeHtml(label) +
    '</div><div class="v">' +
    escapeHtml(value) +
    "</div></div>"
  );
}

function renderPod(pod) {
  const cls = pod.scheduled ? "scheduled" : "unscheduled";
  let html = '<article class="pod-card ' + cls + '">';
  html += '<div class="pod-top"><span class="pod-name">' + escapeHtml(pod.name) + "</span>";
  if (pod.scheduled) {
    html += '<span class="pod-node">' + escapeHtml(pod.node) + "</span>";
  } else {
    html += '<span class="pod-node" style="color:#ffb4ae;background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.35)">未排上</span>';
  }
  html += "</div>";

  if (pod.scheduled) {
    html +=
      '<p class="pod-reason" style="color:#8b97a8">候选节点：' +
      pod.candidate_nodes.map(escapeHtml).join(" / ") +
      "</p>";
    html += '<table class="claim-table"><thead><tr><th>PVC</th><th>StorageClass</th><th class="num">申请</th><th>分配 PV</th></tr></thead><tbody>';
    for (const a of pod.assignments) {
      html +=
        "<tr><td>" + escapeHtml(a.claim) + "</td><td class='sc'>" +
        escapeHtml(a.storage_class) + "</td><td class='num cap'>" +
        escapeHtml(a.capacity_gi) + "Gi</td><td class='pv'>" +
        escapeHtml(a.pv) + "</td></tr>";
    }
    html += "</tbody></table>";
  } else {
    html += '<p class="pod-reason">' + escapeHtml(pod.reason) + "</p>";
    html +=
      '<p class="pod-reason" style="color:#8b97a8">候选节点：' +
      pod.candidate_nodes.map(escapeHtml).join(" / ") +
      "；需要 " + pod.claims.length + " 项独占 PVC</p>";
  }
  html += "</article>";
  return html;
}

function render(data) {
  const s = data.summary;
  $("#summary-grid").innerHTML =
    stat("可同时启动", s.scheduled_count + " / " + s.pod_count, s.scheduled_count ? "good" : "bad") +
    stat("未排上", s.unscheduled_count, s.unscheduled_count ? "bad" : "good") +
    stat("已分配 PVC", s.assigned_claim_count + " / " + s.claim_count) +
    stat("PV 总数", s.pv_count);

  const warningsEl = $("#warnings");
  if (data.warnings && data.warnings.length) {
    warningsEl.hidden = false;
    warningsEl.textContent = data.warnings.join(" ");
  } else {
    warningsEl.hidden = true;
  }

  const scheduledPods = data.pods.filter((p) => p.scheduled);
  const unscheduledPods = data.pods.filter((p) => !p.scheduled);

  $("#scheduled-list").innerHTML = scheduledPods.length
    ? scheduledPods.map(renderPod).join("")
    : '<div class="empty">' +
      (data.summary.pod_count ? "没有 Pod 能被排上。" : "清单中没有待启动的 Pod。") +
      "</div>";

  $("#unscheduled-list").innerHTML = unscheduledPods.length
    ? unscheduledPods.map(renderPod).join("")
    : '<div class="empty">' +
      (data.summary.pod_count ? "全部 Pod 都能启动 🎉" : "没有待启动的 Pod。") +
      "</div>";

  const tbody = $("#pv-table tbody");
  tbody.innerHTML = data.pvs
    .map((pv) => {
      const used = pv.in_use;
      const owner = used
        ? '<span class="owner">' +
          escapeHtml(pv.used_by.pod) + " / " + escapeHtml(pv.used_by.claim) +
          "</span>"
        : '<span class="free">空闲</span>';
      return (
        '<tr class="' + (used ? "used" : "") + '"><td class="mono">' +
        escapeHtml(pv.name) + "</td><td>" + escapeHtml(pv.node) +
        "</td><td>" + escapeHtml(pv.storage_class) +
        "</td><td class='num'>" + escapeHtml(pv.capacity_gi) + " Gi</td><td>" +
        owner + "</td></tr>"
      );
    })
    .join("");

  resultsEl.hidden = false;
}

loadSampleBtn.addEventListener("click", async () => {
  loadSampleBtn.disabled = true;
  try {
    const resp = await fetch("/api/sample");
    const data = await resp.json();
    jsonInput.value = data.json_text;
    setStatus("已载入争用样例，点击「开始试算」");
    await runPlan(jsonInput.value);
  } catch (err) {
    setStatus("样例载入失败：" + err.message, "error");
  } finally {
    loadSampleBtn.disabled = false;
  }
});

runBtn.addEventListener("click", () => {
  const text = jsonInput.value.trim();
  if (!text) {
    showErrors(["请先粘贴或上传 JSON 清单"]);
    setStatus("缺少输入", "error");
    return;
  }
  runPlan(text);
});

clearBtn.addEventListener("click", () => {
  jsonInput.value = "";
  clearErrors();
  resultsEl.hidden = true;
  emptyState.hidden = false;
  badge.textContent = "尚未试算";
  badge.className = "badge muted";
  setStatus("");
});

fileInput.addEventListener("change", async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  try {
    jsonInput.value = await file.text();
    setStatus("已载入 " + file.name);
    await runPlan(jsonInput.value);
  } catch (err) {
    setStatus("读取文件失败：" + err.message, "error");
  } finally {
    fileInput.value = "";
  }
});

// 首次打开自动带样例，方便直接试算。
loadSampleBtn.click();
