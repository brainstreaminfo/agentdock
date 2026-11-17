// Small global helpers. Keep JavaScript to a minimum: HTMX + Alpine do most work.

// Show a readable message when an HTMX request fails, instead of failing silently.
document.addEventListener("htmx:responseError", function (event) {
  var status = event.detail.xhr.status;
  if (status === 409 || status === 400) return; // the server sent a friendly inline message
  console.error("HTMX request failed", status, event.detail.requestConfig.path);
  alert(status === 403 ? "You don't have permission to do that." : "Something went wrong (" + status + "). Please try again.");
});

// ---------------------------------------------------------------- charts
// <canvas data-chart='{"type": "bar", ...}'> -> Chart.js chart.
function initCharts(root) {
  if (typeof Chart === "undefined") return;
  (root || document).querySelectorAll("canvas[data-chart]:not([data-ready])").forEach(function (canvas) {
    canvas.dataset.ready = "1";
    try { new Chart(canvas, JSON.parse(canvas.dataset.chart)); }
    catch (e) { console.error("Chart failed", e); }
  });
}

// ---------------------------------------------------------------- live agent runs
// Elements with data-stream="<url>" listen to Server-Sent Events from a run:
//   status -> small "Thinking…" line      delta -> append answer text
//   tool   -> add/update a tool chip       done / failed -> replace with final answer
function connectRun(el) {
  if (el.dataset.connected) return;
  el.dataset.connected = "1";
  var text = el.querySelector("[data-live-text]");
  var tools = el.querySelector("[data-live-tools]");
  var status = el.querySelector("[data-live-status]");
  var source = new EventSource(el.dataset.stream);

  source.addEventListener("status", function (e) { status.textContent = JSON.parse(e.data).text; });
  source.addEventListener("delta", function (e) {
    text.insertAdjacentHTML("beforeend", JSON.parse(e.data).html);
    status.textContent = "";
    scrollChat();
  });
  source.addEventListener("tool", function (e) {
    var d = JSON.parse(e.data);
    var existing = tools.querySelector('[data-call="' + d.call_id + '"]');
    var wrapper = document.createElement("span");
    wrapper.dataset.call = d.call_id;
    wrapper.innerHTML = d.html;
    if (existing) existing.replaceWith(wrapper); else tools.appendChild(wrapper);
    status.textContent = d.status === "running" ? "Working…" : status.textContent;
  });
  function finish(e) {
    source.close();
    var d = JSON.parse(e.data || "{}");
    if (!d.html) { window.location.reload(); return; }
    var holder = document.createElement("div");
    holder.innerHTML = d.html;
    var finished = holder.firstElementChild;
    el.replaceWith(finished);
    if (window.htmx) htmx.process(finished);   // activate Approve / Reject buttons
    initCharts(finished);
    scrollChat();
  }
  source.addEventListener("done", finish);
  source.addEventListener("failed", finish);
}

function scrollChat() {
  var turns = document.getElementById("turns");
  if (turns) turns.scrollTop = turns.scrollHeight;
}

function initPage(root) {
  (root || document).querySelectorAll("[data-stream]").forEach(connectRun);
  initCharts(root);
}

document.addEventListener("DOMContentLoaded", function () { initPage(document); scrollChat(); });
document.addEventListener("htmx:afterSwap", function (e) { initPage(e.target); scrollChat(); });
