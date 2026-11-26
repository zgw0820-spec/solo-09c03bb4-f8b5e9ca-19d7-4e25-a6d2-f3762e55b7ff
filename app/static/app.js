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
const minPodsInput = $("#min-pods");
const timeLimitInput = $("#time-limit");

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

function optionPayload(sourceText) {
  const body = { json_text: sourceText };
  const minPods = minPodsInput.value.trim();
  const timeLimit = timeLimitInput.value.trim();
  if (minPods !== "") body.min_pods = Number(minPods);
  if (timeLimit !== "") body.time_limit_ms = Number(timeLimit);
  return body;
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
      body: JSON.stringify(optionPayload(sourceText)),
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
      "完成：可启动 " +
        s.lower_bound +
        " 个（上界 " +
        s.upper_bound +
        " 个），共 " +
        s.pod_count +
        " 个 Pod" +
        (s.timed_out ? "，已到时停止" : "")
    );
    if (s.optimal) {
      badge.textContent = "已证最优";
      badge.className = "badge ok";
    } else {
      badge.textContent = "未证最优 · 待确认";
      badge.className = "badge warn";
    }
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
  const cls = pod.scheduled ? "scheduled" : pod.status === "unconfirmed" ? "unconfirmed" : "unscheduled";
  let html = '<article class="pod-card ' + cls + '">';
  html += '<div class="pod-top"><span class="pod-name">' + escapeHtml(pod.name) + "</span>";
  if (pod.scheduled) {
    html += '<span class="pod-node">' + escapeHtml(pod.node) + "</span>";
  } else {
    const label = pod.status === "unconfirmed" ? "待确认" : "未排上";
    html += '<span class="pod-node pod-node-' + escapeHtml(pod.status) + '">' + label + "</span>";
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

function renderReleaseBanner(data) {
  const banner = $("#release-banner");
  const release = data.release;
  if (!release || release.min_pods === null) {
    banner.hidden = true;
    banner.textContent = "";
    return;
  }
  const labelMap = {
    go: ["可上线", "release-go"],
    postpone: ["建议推迟", "release-postpone"],
    review: ["待复核", "release-review"],
  };
  const [label, cls] = labelMap[release.status] || ["待复核", "release-review"];
  banner.className = "release-banner " + cls;
  banner.innerHTML =
    '<span class="release-tag">' +
    label +
    "</span><span class=\"release-text\">" +
    escapeHtml(release.status_text) +
    "</span>";
  banner.hidden = false;
}

function renderSearchInfo(data) {
  const s = data.search;
  const parts = [];
  parts.push(
    s.optimal
      ? "已完整试算，上下界相等（" + s.lower_bound + "），结果为最优"
      : "时限内停止搜索，未证最优：下界 " + s.lower_bound + " / 上界 " + s.upper_bound
  );
  if (s.time_limit_ms !== null) {
    parts.push("时限 " + s.time_limit_ms + " ms");
  }
  parts.push("实际耗时 " + s.elapsed_ms + " ms");
  $("#search-info").innerHTML = parts
    .map((p) => "<span>" + escapeHtml(p) + "</span>")
    .join("");
}

function render(data) {
  const s = data.summary;
  renderReleaseBanner(data);
  renderSearchInfo(data);

  $("#summary-grid").innerHTML =
    stat("可启动（下界）", s.lower_bound + " / " + s.pod_count, s.lower_bound ? "good" : "bad") +
    stat("可启动上界", s.upper_bound, s.optimal ? "good" : "warn") +
    stat("最优性", s.optimal ? "已证最优" : "未证最优", s.optimal ? "good" : "warn") +
    stat("已分配 PVC", s.assigned_claim_count + " / " + s.claim_count) +
    stat("未排上", s.unscheduled_count, s.unscheduled_count ? "bad" : "good") +
    stat(
      "待确认",
      s.unconfirmed_count,
      s.unconfirmed_count ? "warn" : "good"
    ) +
    stat("PV 总数", s.pv_count) +
    stat("Pod 总数", s.pod_count);

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

  const unconfirmedCount = unscheduledPods.filter((p) => p.status === "unconfirmed").length;
  $("#unscheduled-title").textContent = unconfirmedCount
    ? "未排上 / 待确认的 Pod（未证最优，待确认项可能在更长时限后排上）"
    : "未排上的 Pod";
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
  const minPods = minPodsInput.value.trim();
  const timeLimit = timeLimitInput.value.trim();
  const localErrors = [];
  if (minPods !== "" && (!/^\d+$/.test(minPods) || Number(minPods) < 0)) {
    localErrors.push("最低上线 Pod 数必须是非负整数");
  }
  if (timeLimit !== "" && (!/^\d+$/.test(timeLimit) || Number(timeLimit) <= 0)) {
    localErrors.push("求解时限（毫秒）必须是正整数");
  }
  if (localErrors.length) {
    showErrors(localErrors);
    setStatus("参数有误", "error");
    return;
  }
  runPlan(text);
});

clearBtn.addEventListener("click", () => {
  jsonInput.value = "";
  minPodsInput.value = "";
  timeLimitInput.value = "";
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
