// Render dashboard charts from the embedded JSON island. Runs on first load and
// after every HTMX swap; chart instances are destroyed before re-creating so
// repeated filter changes never leak canvases.
(function () {
  "use strict";
  var grid = "#2a2f3d";
  var text = "#9aa3b2";
  var accent = "#6ea8fe";
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

  function renderCharts() {
    if (typeof Chart === "undefined") {
      return;
    }
    Chart.defaults.color = text;
    Chart.defaults.borderColor = grid;
    renderSessionChart();
    renderContextAnalysisChart();
    var node = document.getElementById("report-data");
    if (!node) {
      return;
    }
    var data = JSON.parse(node.textContent);
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
  }

  function renderContextAnalysisChart() {
    var node = document.getElementById("context-analysis-data");
    if (!node || typeof Chart === "undefined") {
      return;
    }
    var data = JSON.parse(node.textContent);
    var palette = ["#6ea8fe", "#7ee2b8", "#f6c177", "#c4a7e7", "#eb6f92", "#9ccfd8", "#f4b8e4", "#a6da95"];
    var datasets = data.categories.map(function (category, index) {
      return {
        type: "bar", label: category.label, data: category.values,
        backgroundColor: palette[index % palette.length], stack: "visible", yAxisID: "bytes",
      };
    });
    datasets.push({
      type: "line", label: "Context tokens", data: data.context_tokens,
      borderColor: "#ffffff", backgroundColor: "#ffffff", yAxisID: "tokens", tension: 0.15,
      pointRadius: data.compactions.map(function (value) { return value ? 6 : 2; }),
      pointBackgroundColor: data.compactions.map(function (value) { return value ? "#eb6f92" : "#ffffff"; }),
    });
    datasets.push({
      type: "line", label: "Fresh input tokens", data: data.input_tokens,
      borderColor: "#6ea8fe", backgroundColor: "#6ea8fe", yAxisID: "tokens", tension: 0.15,
      borderDash: [2, 3], pointRadius: 1,
    });
    datasets.push({
      type: "line", label: "Cache read tokens", data: data.cache_read_tokens,
      borderColor: "#7ee2b8", backgroundColor: "#7ee2b8", yAxisID: "tokens", tension: 0.15,
      borderDash: [5, 4], pointRadius: 1,
    });
    datasets.push({
      type: "line", label: "Cache write tokens", data: data.cache_creation_tokens,
      borderColor: "#f6c177", backgroundColor: "#f6c177", yAxisID: "tokens", tension: 0.15,
      borderDash: [3, 5], pointRadius: 1,
    });
    datasets.push({
      type: "line", label: "Context window utilization",
      data: data.utilization.map(function (value) { return value === null ? null : value * 100; }),
      borderColor: "#eb6f92", backgroundColor: "#eb6f92", yAxisID: "utilization",
      tension: 0.15, borderDash: [8, 4], pointRadius: 1,
    });
    draw("contextCompositionChart", {
      data: { labels: data.labels, datasets: datasets },
      options: {
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { stacked: true, title: { display: true, text: "model call" } },
          bytes: { stacked: true, position: "left", title: { display: true, text: "observable bytes" } },
          tokens: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "tokens" } },
          utilization: { position: "right", min: 0, grid: { drawOnChartArea: false },
            title: { display: true, text: "context window %" },
            ticks: { callback: function (value) { return value + "%"; } } },
        },
      },
    });
  }

  // Per-session context growth line with compaction drop markers; the island is
  // only present on the session detail page.
  function renderSessionChart() {
    var node = document.getElementById("session-chart-data");
    if (!node || typeof Chart === "undefined") {
      return;
    }
    var data = JSON.parse(node.textContent);
    var curve = data.context_curve;
    if (!curve || !curve.points.length) {
      return;
    }
    var labels = curve.points.map(function (p) { return p.idx; });
    var compactionIdx = {};
    curve.compactions.forEach(function (c) { compactionIdx[c.idx] = true; });
    draw("contextChart", {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          { label: "Context tokens", data: curve.points.map(function (p) { return p.context; }),
            borderColor: accent, backgroundColor: accent, tension: 0.1, pointRadius: 2 },
          { label: "Output tokens", data: curve.points.map(function (p) { return p.output; }),
            borderColor: "#7ee2b8", backgroundColor: "#7ee2b8", tension: 0.1, pointRadius: 2 },
        ],
      },
      options: {
        scales: { x: { title: { display: true, text: "assistant turn (message idx)" } } },
        plugins: {
          tooltip: {
            callbacks: {
              footer: function (items) {
                return items.some(function (i) { return compactionIdx[i.label]; })
                  ? "compaction near this turn" : "";
              },
            },
          },
        },
      },
    });
  }

  document.addEventListener("DOMContentLoaded", renderCharts);
  document.body.addEventListener("htmx:afterSwap", renderCharts);
})();
