// Render report charts from the embedded JSON island. Idempotent: existing
// chart instances are destroyed before re-creating.
(function () {
  "use strict";
  var node = document.getElementById("report-data");
  if (!node || typeof Chart === "undefined") {
    return;
  }
  var data = JSON.parse(node.textContent);
  var grid = "#2a2f3d";
  var text = "#9aa3b2";
  var accent = "#6ea8fe";
  Chart.defaults.color = text;
  Chart.defaults.borderColor = grid;

  var instances = {};
  function draw(id, config) {
    var el = document.getElementById(id);
    if (!el) {
      return;
    }
    if (instances[id]) {
      instances[id].destroy();
    }
    instances[id] = new Chart(el, config);
  }

  var avail = data.availability || {};

  if (data.activity && data.activity.labels.length) {
    var activityDatasets;
    var bothAgents = avail.claude && avail.codex && data.activity.claude && data.activity.codex;
    if (bothAgents) {
      activityDatasets = [
        { label: "Claude", data: data.activity.claude, borderColor: accent,
          backgroundColor: accent, tension: 0.25 },
        { label: "Codex", data: data.activity.codex, borderColor: "#7ee2b8",
          backgroundColor: "#7ee2b8", tension: 0.25 },
      ];
    } else {
      activityDatasets = [
        { label: "Sessions", data: data.activity.sessions, borderColor: accent,
          backgroundColor: accent, tension: 0.25 },
      ];
    }
    draw("activityChart", {
      type: "line",
      data: { labels: data.activity.labels, datasets: activityDatasets },
      options: { plugins: { legend: { display: Boolean(bothAgents) } } },
    });
  }

  if (data.token_breakdown) {
    draw("tokenChart", {
      type: "doughnut",
      data: {
        labels: data.token_breakdown.labels,
        datasets: [
          { data: data.token_breakdown.values,
            backgroundColor: ["#6ea8fe", "#7ee2b8", "#f6c177", "#c4a7e7"] },
        ],
      },
    });
  }

  if (data.tools && data.tools.labels.length) {
    draw("toolChart", {
      type: "bar",
      data: {
        labels: data.tools.labels,
        datasets: [
          { label: "Calls", data: data.tools.calls, backgroundColor: accent },
          { label: "Errors", data: data.tools.errors, backgroundColor: "#eb6f92" },
        ],
      },
    });
  }

  if (data.models && data.models.labels.length) {
    draw("modelChart", {
      type: "bar",
      data: {
        labels: data.models.labels,
        datasets: [
          { label: "Tokens", data: data.models.tokens, backgroundColor: "#7ee2b8" },
        ],
      },
      options: { indexAxis: "y", plugins: { legend: { display: false } } },
    });
  }

  if (data.cache_trend && data.cache_trend.labels.length) {
    var cacheDatasets = [
      { label: "Cache read", data: data.cache_trend.cache_read,
        borderColor: "#7ee2b8", backgroundColor: "#7ee2b8", tension: 0.25 },
    ];
    if (avail.cache_creation) {
      cacheDatasets.push(
        { label: "Cache write (claude only)", data: data.cache_trend.cache_creation,
          borderColor: "#f6c177", backgroundColor: "#f6c177", tension: 0.25 });
    }
    cacheDatasets.push(
      { label: "Fresh input", data: data.cache_trend.input,
        borderColor: accent, backgroundColor: accent, tension: 0.25 });
    draw("cacheChart", {
      type: "line",
      data: { labels: data.cache_trend.labels, datasets: cacheDatasets },
      options: { scales: { y: { type: "logarithmic" } } },
    });
  }

  if (data.cache_misses && data.cache_misses.values.some(function (v) { return v > 0; })) {
    draw("missChart", {
      type: "doughnut",
      data: {
        labels: data.cache_misses.labels,
        datasets: [
          { data: data.cache_misses.values,
            backgroundColor: ["#c4a7e7", "#f6c177", "#eb6f92", "#9aa3b2"] },
        ],
      },
    });
  }
})();
