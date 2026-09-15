/* ─── State ─────────────────────────────────────────────────────────────── */
let profileResults = [];
let keywordResults = [];
let comboResults = [];
let activeResultTab = "profiles";

/* ─── DOM ───────────────────────────────────────────────────────────────── */
const btnRun = document.getElementById("btnRun");
const statusBar = document.getElementById("statusBar");
const statusText = document.getElementById("statusText");
const errorBox = document.getElementById("errorBox");
const errorMsg = document.getElementById("errorMsg");
const resultsSection = document.getElementById("results");
const totalCount = document.getElementById("totalCount");
const postsContainer = document.getElementById("postsContainer");
const btnDownloadExcel = document.getElementById("btnDownloadExcel");
const inputFile = document.getElementById("inputFile");
const preview = document.getElementById("preview");
const previewCount = document.getElementById("previewCount");
const previewList = document.getElementById("previewList");

/* ─── File preview on select ────────────────────────────────────────────── */
inputFile.addEventListener("change", async () => {
  if (!inputFile.files.length) return;
  const profilesTab = document.getElementById("profilesTabName").value.trim() || "profiles";
  const keywordsTab = document.getElementById("keywordsTabName").value.trim() || "keywords";

  const fd = new FormData();
  fd.append("file", inputFile.files[0]);
  fd.append("profilesTab", profilesTab);
  fd.append("keywordsTab", keywordsTab);

  // Quick preview: read both tabs
  try {
    // Read profiles
    const fd1 = new FormData();
    fd1.append("file", inputFile.files[0]);
    fd1.append("column", "1");
    fd1.append("sheet_name", profilesTab);
    const r1 = await fetch("/api/upload-excel", { method: "POST", body: fd1 });
    const d1 = await r1.json();

    // Read keywords (re-read file)
    const fd2 = new FormData();
    fd2.append("file", inputFile.files[0]);
    fd2.append("column", "1");
    fd2.append("sheet_name", keywordsTab);
    const r2 = await fetch("/api/upload-excel", { method: "POST", body: fd2 });
    const d2 = await r2.json();

    const pCount = d1.error ? 0 : d1.count;
    const kCount = d2.error ? 0 : d2.count;

    preview.hidden = false;
    previewCount.textContent = `${pCount} profiles, ${kCount} keywords loaded`;
    let html = "";
    if (pCount) html += `<div class="preview-item"><strong>Profiles:</strong> ${d1.values.slice(0,3).join(", ")}${pCount > 3 ? ` …+${pCount-3} more` : ""}</div>`;
    if (kCount) html += `<div class="preview-item"><strong>Keywords:</strong> ${d2.values.slice(0,3).join(", ")}${kCount > 3 ? ` …+${kCount-3} more` : ""}</div>`;
    if (!pCount && !kCount) html = `<div class="preview-item muted">No data found in tabs "${profilesTab}" / "${keywordsTab}"</div>`;
    previewList.innerHTML = html;
  } catch (e) {
    preview.hidden = false;
    previewCount.textContent = "File loaded";
    previewList.innerHTML = `<div class="preview-item">${inputFile.files[0].name}</div>`;
  }
});

/* ─── Run all scrapers ──────────────────────────────────────────────────── */
btnRun.addEventListener("click", runAll);

async function runAll() {
  if (!inputFile.files.length) { showError("Please upload an Excel file first."); return; }

  const profilesTab = document.getElementById("profilesTabName").value.trim() || "profiles";
  const keywordsTab = document.getElementById("keywordsTabName").value.trim() || "keywords";
  const maxPosts = document.getElementById("maxPosts").value;
  const kwLimit = document.getElementById("kwLimit").value;
  const kwDate = document.getElementById("kwDate").value;

  const fd = new FormData();
  fd.append("file", inputFile.files[0]);
  fd.append("profilesTab", profilesTab);
  fd.append("keywordsTab", keywordsTab);
  fd.append("maxPosts", maxPosts);
  fd.append("perKeyword", kwLimit);
  fd.append("kwDate", kwDate);

  hideError(); setLoading(true); showStatus("Starting…");
  hideResults();
  profileResults = []; keywordResults = []; comboResults = [];

  try {
    const resp = await fetch("/api/run-all", { method: "POST", body: fd });
    if (!resp.ok) { throw new Error((await resp.json()).error || "Server error"); }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload) handleMsg(JSON.parse(payload));
      }
    }
  } catch (err) { showError(err.message); }
  setLoading(false); hideStatus();
}

function handleMsg(msg) {
  if (msg.status === "running") showStatus(msg.step || "Working…");
  if (msg.status === "profileResults") {
    profileResults = msg.posts || [];
    document.getElementById("profileResultCount").textContent = `(${profileResults.length})`;
    renderCurrentTab();
  }
  if (msg.status === "keywordResults") {
    keywordResults = msg.posts || [];
    document.getElementById("keywordResultCount").textContent = `(${keywordResults.length})`;
    renderCurrentTab();
  }
  if (msg.status === "comboResults") {
    comboResults = msg.posts || [];
    document.getElementById("comboResultCount").textContent = `(${comboResults.length})`;
    renderCurrentTab();
  }
  if (msg.status === "done") {
    const total = profileResults.length + keywordResults.length + comboResults.length;
    totalCount.textContent = `${total} total`;
    resultsSection.hidden = false;
  }
  if (msg.status === "error") showError(msg.message);
}

/* ─── Result tabs ───────────────────────────────────────────────────────── */
document.querySelectorAll("[data-result-tab]").forEach(tab => {
  tab.addEventListener("click", () => {
    activeResultTab = tab.dataset.resultTab;
    document.querySelectorAll("[data-result-tab]").forEach(t => t.classList.toggle("active", t === tab));
    renderCurrentTab();
  });
});

function renderCurrentTab() {
  resultsSection.hidden = false;
  postsContainer.innerHTML = "";
  let posts = [];
  if (activeResultTab === "profiles") posts = profileResults;
  else if (activeResultTab === "keywords") posts = keywordResults;
  else posts = comboResults;

  if (!posts.length) { postsContainer.appendChild(emptyState()); return; }
  posts.forEach(post => postsContainer.appendChild(buildPostCard(post)));
}

/* ─── Download Excel with 3 tabs ────────────────────────────────────────── */
btnDownloadExcel.addEventListener("click", async () => {
  const makeRows = (posts) => posts.map(p => ({
    authorName: p.authorName || "",
    postedAt: p.postedAt || "",
    text: (p.text || "").substring(0, 32000),
    postUrl: p.postUrl || "",
    reactions: p.reactionsCount || 0,
    comments: p.commentsCount || 0,
    shares: p.sharesCount || 0,
    keyword: p.keyword || "",
  }));

  const body = {
    profiles: makeRows(profileResults),
    keywords: makeRows(keywordResults),
    combo: makeRows(comboResults),
  };

  try {
    const resp = await fetch("/api/download-results", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) { showError((await resp.json()).error || "Download failed."); return; }
    const blob = await resp.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = "linkedin_results.xlsx";
    document.body.appendChild(a); a.click(); a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) { showError(err.message); }
});

/* ─── Post card ─────────────────────────────────────────────────────────── */
function buildPostCard(post) {
  const card = document.createElement("article"); card.className = "post-card";
  const initials = (post.authorName || "?").split(" ").slice(0, 2).map(w => w[0]).join("").toUpperCase();
  const avatarInner = post.authorImage
    ? `<img src="${escapeAttr(typeof post.authorImage === 'object' ? post.authorImage.url || '' : post.authorImage)}" alt="" loading="lazy" onerror="this.style.display='none'" />`
    : initials;
  const authorHref = post.authorUrl ? `href="${escapeAttr(post.authorUrl)}" target="_blank"` : "";
  const postLink = post.postUrl ? `<a class="post-link-btn" href="${escapeAttr(post.postUrl)}" target="_blank">View</a>` : "";
  const dateStr = formatDate(post);
  const hasEng = post.reactionsCount || post.commentsCount || post.sharesCount;
  const engHTML = hasEng ? `<div class="engagement">
    ${post.reactionsCount ? `<span class="eng-pill">👍 ${fmtNum(post.reactionsCount)}</span>` : ""}
    ${post.commentsCount ? `<span class="eng-pill">💬 ${fmtNum(post.commentsCount)}</span>` : ""}
    ${post.sharesCount ? `<span class="eng-pill">🔁 ${fmtNum(post.sharesCount)}</span>` : ""}
  </div>` : "";
  const text = post.text || "";
  const isLong = text.length > 320;
  const kwBadge = post.keyword ? `<div class="keyword-badge">🔑 ${escapeHtml(post.keyword)}</div>` : "";

  card.innerHTML = `
    <div class="post-header">
      <div class="author-avatar">${avatarInner}</div>
      <div class="author-info">
        <a class="author-name" ${authorHref}>${escapeHtml(post.authorName || "Unknown")}</a>
        ${dateStr ? `<div class="post-date">${dateStr}</div>` : ""}
      </div>
      ${postLink}
    </div>
    ${text ? `<div class="post-text ${isLong ? "collapsed" : ""}">${escapeHtml(text)}</div>
    ${isLong ? `<button class="toggle-expand" type="button">Show more</button>` : ""}` : ""}
    ${engHTML}
    ${kwBadge}`;

  if (isLong) {
    const btn = card.querySelector(".toggle-expand");
    const el = card.querySelector(".post-text");
    btn.addEventListener("click", () => { const c = el.classList.toggle("collapsed"); btn.textContent = c ? "Show more" : "Show less"; });
  }
  return card;
}

function emptyState() {
  const el = document.createElement("div"); el.className = "no-posts";
  el.innerHTML = `<div class="empty-icon">🔍</div><p>No posts found.</p>`;
  return el;
}

/* ─── UI helpers ────────────────────────────────────────────────────────── */
function setLoading(on) { btnRun.disabled = on; btnRun.querySelector(".btn-text").hidden = on; btnRun.querySelector(".btn-spinner").hidden = !on; }
function showStatus(msg) { statusText.textContent = msg; statusBar.hidden = false; }
function hideStatus() { statusBar.hidden = true; }
function showError(msg) { errorMsg.textContent = msg; errorBox.hidden = false; }
function hideError() { errorBox.hidden = true; }
function hideResults() { resultsSection.hidden = true; postsContainer.innerHTML = ""; }

/* ─── Utils ─────────────────────────────────────────────────────────────── */
function formatDate(post) {
  const ms = post.timestampMs;
  if (ms && ms > 0) return new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  const raw = post.postedAt; if (!raw) return "";
  const d = new Date(raw); if (!isNaN(d)) return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
  return raw;
}
function fmtNum(n) { return n >= 1000 ? (n/1000).toFixed(1).replace(/\.0$/,"") + "k" : String(n); }
function escapeHtml(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function escapeAttr(s) { return String(s).replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }


/* ─── Input source toggle (Excel vs Google Sheet) ───────────────────────── */
const srcExcel = document.getElementById("srcExcel");
const srcSheet = document.getElementById("srcSheet");
const excelInput = document.getElementById("excelInput");
const sheetInput = document.getElementById("sheetInput");

srcExcel.addEventListener("click", () => {
  srcExcel.classList.add("active"); srcSheet.classList.remove("active");
  excelInput.hidden = false; sheetInput.hidden = true;
});
srcSheet.addEventListener("click", () => {
  srcSheet.classList.add("active"); srcExcel.classList.remove("active");
  sheetInput.hidden = false; excelInput.hidden = true;
});

/* ─── Google Sheet input: load on URL paste ─────────────────────────────── */
document.getElementById("inputSheetUrl").addEventListener("change", async function() {
  const url = this.value.trim();
  if (!url) return;
  const profilesTab = document.getElementById("sheetProfilesTab").value.trim() || "profiles";
  const keywordsTab = document.getElementById("sheetKeywordsTab").value.trim() || "keywords";

  showStatus("Loading from Google Sheet…");
  try {
    const [r1, r2] = await Promise.all([
      fetch("/api/read-sheet", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({sheetUrl:url, sheetName:profilesTab, column:1}) }),
      fetch("/api/read-sheet", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({sheetUrl:url, sheetName:keywordsTab, column:1}) }),
    ]);
    const d1 = await r1.json();
    const d2 = await r2.json();
    const pCount = d1.error ? 0 : d1.count;
    const kCount = d2.error ? 0 : d2.count;

    preview.hidden = false;
    previewCount.textContent = `${pCount} profiles, ${kCount} keywords loaded from sheet`;
    let html = "";
    if (pCount) html += `<div class="preview-item"><strong>Profiles:</strong> ${d1.values.slice(0,3).join(", ")}${pCount>3?` …+${pCount-3} more`:""}</div>`;
    if (kCount) html += `<div class="preview-item"><strong>Keywords:</strong> ${d2.values.slice(0,3).join(", ")}${kCount>3?` …+${kCount-3} more`:""}</div>`;
    if (!pCount && !kCount) html = `<div class="preview-item muted">No data found in tabs "${profilesTab}" / "${keywordsTab}"</div>`;
    previewList.innerHTML = html;
    hideError(); hideStatus();
  } catch(e) { showError(e.message); hideStatus(); }
});

/* ─── Override runAll to support Google Sheet input ──────────────────────── */
const originalRunAll = runAll;
btnRun.removeEventListener("click", originalRunAll);
btnRun.addEventListener("click", async () => {
  const isSheet = srcSheet.classList.contains("active");

  if (isSheet) {
    // Google Sheet mode — read from sheet then call run-all-json
    const sheetUrl = document.getElementById("inputSheetUrl").value.trim();
    if (!sheetUrl) { showError("Please enter a Google Sheet URL."); return; }
    const profilesTab = document.getElementById("sheetProfilesTab").value.trim() || "profiles";
    const keywordsTab = document.getElementById("sheetKeywordsTab").value.trim() || "keywords";
    const maxPosts = document.getElementById("maxPosts").value;
    const kwLimit = document.getElementById("kwLimit").value;
    const kwDate = document.getElementById("kwDate").value;

    hideError(); setLoading(true); showStatus("Loading data from Google Sheet…");

    try {
      const [r1, r2] = await Promise.all([
        fetch("/api/read-sheet", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sheetUrl, sheetName:profilesTab, column:1}) }),
        fetch("/api/read-sheet", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({sheetUrl, sheetName:keywordsTab, column:1}) }),
      ]);
      const d1 = await r1.json();
      const d2 = await r2.json();
      if (d1.error && d2.error) { showError(`Profiles: ${d1.error}\nKeywords: ${d2.error}`); setLoading(false); hideStatus(); return; }

      const profiles = d1.values || [];
      const keywords = d2.values || [];
      if (!profiles.length && !keywords.length) { showError("No data found in the sheet."); setLoading(false); hideStatus(); return; }

      // Call the JSON version of run-all
      showStatus("Starting scrapers…");
      hideResults();
      profileResults = []; keywordResults = []; comboResults = [];

      const resp = await fetch("/api/run-all-json", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profiles, keywords, maxPosts: parseInt(maxPosts), perKeyword: parseInt(kwLimit), kwDate }),
      });
      if (!resp.ok) throw new Error((await resp.json()).error || "Server error");

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (payload) handleMsg(JSON.parse(payload));
        }
      }
    } catch(err) { showError(err.message); }
    setLoading(false); hideStatus();
  } else {
    // Excel mode — original flow
    await runAll();
  }
});

/* ─── Export to Google Sheet ─────────────────────────────────────────────── */
const btnExportSheet = document.getElementById("btnExportSheet");
const exportModal = document.getElementById("exportModal");
const btnCloseModal = document.getElementById("btnCloseModal");
const btnDoExport = document.getElementById("btnDoExport");
const exportStatus = document.getElementById("exportStatus");

btnExportSheet.addEventListener("click", () => { exportModal.hidden = false; });
btnCloseModal.addEventListener("click", () => { exportModal.hidden = true; exportStatus.hidden = true; });
exportModal.addEventListener("click", e => { if (e.target === exportModal) { exportModal.hidden = true; exportStatus.hidden = true; } });

btnDoExport.addEventListener("click", async () => {
  const sheetUrl = document.getElementById("exportSheetUrl").value.trim();
  if (!sheetUrl) { showExportStatus("Please enter a Google Sheet URL.", "error"); return; }

  const makeRows = (posts) => posts.map(p => ({
    authorName: p.authorName || "", postedAt: p.postedAt || "",
    text: (p.text || "").substring(0, 5000), postUrl: p.postUrl || "",
    reactions: p.reactionsCount || 0, comments: p.commentsCount || 0, shares: p.sharesCount || 0,
    keyword: p.keyword || "",
  }));

  setExportLoading(true);
  try {
    // Export each tab
    const tabs = [
      { name: "Profile Posts", data: makeRows(profileResults) },
      { name: "Keyword Posts", data: makeRows(keywordResults) },
      { name: "Profile + Keywords", data: makeRows(comboResults) },
    ];
    for (const tab of tabs) {
      if (!tab.data.length) continue;
      const resp = await fetch("/api/export-sheet", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sheetUrl, sheetName: tab.name, data: tab.data }),
      });
      const result = await resp.json();
      if (result.error) { showExportStatus(`Error on "${tab.name}": ${result.error}`, "error"); setExportLoading(false); return; }
    }
    showExportStatus("✓ Exported all results to 3 tabs in your Google Sheet!", "success");
  } catch(err) { showExportStatus(err.message, "error"); }
  setExportLoading(false);
});

function showExportStatus(msg, type) { exportStatus.hidden = false; exportStatus.textContent = msg; exportStatus.className = "export-status " + type; }
function setExportLoading(on) { btnDoExport.disabled = on; btnDoExport.querySelector(".btn-text").hidden = on; btnDoExport.querySelector(".btn-spinner").hidden = !on; }


/* ─── Run from permanent Google Sheet ───────────────────────────────────── */
const btnRunPermanent = document.getElementById("btnRunPermanent");

let activeJobId = null;
let pollTimer = null;

btnRunPermanent.addEventListener("click", async () => {
  const maxPosts = document.getElementById("maxPosts").value;
  const perKeyword = document.getElementById("kwLimit").value;
  const kwDate = document.getElementById("kwDate").value;

  hideError(); setPermanentLoading(true); showStatus("Reading from Google Sheet…");
  hideResults();
  profileResults = []; keywordResults = []; comboResults = [];

  try {
    const resp = await fetch("/api/run-permanent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ maxPosts: parseInt(maxPosts), perKeyword: parseInt(perKeyword), kwDate }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Server error");

    activeJobId = data.jobId;
    showStatus(
      `Running ${data.keywords} keywords + ${data.profiles} profiles in the background — ` +
      `about $${data.estimatedCost}. Results are written to the sheet as they finish; ` +
      `you can close this tab.`
    );
    pollJob();
  } catch (err) {
    showError(err.message);
    setPermanentLoading(false); hideStatus();
  }
});

/* The sweep runs for ~15 minutes, well past the request timeout, so we poll. */
async function pollJob() {
  if (!activeJobId) return;
  try {
    const resp = await fetch(`/api/jobs/${activeJobId}`);
    const job = await resp.json();
    if (!resp.ok) throw new Error(job.error || "Lost track of the job");

    const pct = job.keywordsTotal
      ? Math.round((job.keywordsDone / job.keywordsTotal) * 100)
      : 0;
    showStatus(
      `${job.phase} — ${job.keywordsDone}/${job.keywordsTotal} keywords (${pct}%), ` +
      `${job.postsFound} posts so far`
    );

    if (job.status === "done" || job.status === "cancelled") {
      const r = job.result || {};
      showStatus(
        `Finished: ${r.profilePosts || 0} profile posts, ${r.keywordPosts || 0} keyword posts, ` +
        `${r.comboPosts || 0} combo. ${r.keywordsWithResults || 0}/${r.keywordsSearched || 0} ` +
        `keywords returned results` +
        (r.keywordsFailed ? `, ${r.keywordsFailed} failed` : "") +
        `. See the Keyword Report tab in the sheet.`
      );
      stopPolling();
      return;
    }
    if (job.status === "error") {
      showError(job.error || "The run failed.");
      stopPolling();
      return;
    }
    pollTimer = setTimeout(pollJob, 3000);
  } catch (err) {
    showError(err.message);
    stopPolling();
  }
}

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
  activeJobId = null;
  setPermanentLoading(false);
}

function setPermanentLoading(on) {
  btnRunPermanent.disabled = on;
  btnRunPermanent.querySelector(".btn-text").hidden = on;
  btnRunPermanent.querySelector(".btn-spinner").hidden = !on;
}
