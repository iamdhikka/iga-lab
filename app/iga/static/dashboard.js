/* Grafik dashboard. Warna mengikuti peran: hijau = disetujui, merah = ditolak,
   ungu-biru = dibatalkan, amber = masih diproses. Urutan tumpukan sudah divalidasi aman untuk buta warna. */
(function () {
  const data = JSON.parse(document.getElementById("chart-data").textContent);
  const css = getComputedStyle(document.documentElement);
  const ink = css.getPropertyValue("--ink").trim() || "#17212B";
  const muted = css.getPropertyValue("--muted").trim() || "#5B6873";
  const grid = "#E6EBED";
  const surface = "#FFFFFF";
  const COLORS = {
    approved: "#15876F", rejected: "#D2452F", cancelled: "#5866C4", pending: "#C98200",
    joiner: "#15876F", mover: "#5866C4", leaver: "#D2452F",
  };

  Chart.defaults.font.family = '"Plus Jakarta Sans", system-ui, sans-serif';
  Chart.defaults.font.size = 12;
  Chart.defaults.color = muted;
  Chart.defaults.animation = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 300 };

  const tooltip = {
    backgroundColor: ink, titleColor: "#fff", bodyColor: "#DCE3E8", padding: 10, boxPadding: 4,
    cornerRadius: 4, titleFont: { weight: "600" }, usePointStyle: true,
  };
  const axis = (extra) => Object.assign({
    grid: { color: grid, drawTicks: false }, border: { display: false },
    ticks: { padding: 6, color: muted },
  }, extra || {});

  function legend(el, series) {
    if (!el) return;
    el.innerHTML = series.map((s) =>
      `<span><i style="background:${COLORS[s.key]}"></i>${s.label}</span>`).join("");
  }

  function bars(series) {
    return series.map((s) => ({
      label: s.label, data: s.data, backgroundColor: COLORS[s.key],
      borderColor: surface, borderWidth: { top: 2, left: 0, right: 0, bottom: 0 }, borderSkipped: "bottom",
      maxBarThickness: 28, categoryPercentage: 0.8, barPercentage: 0.9,
    }));
  }

  // Permintaan per hari, ditumpuk per hasil
  if (document.getElementById("volume")) {
    legend(document.getElementById("volume-legend"), data.volume.series);
    new Chart(document.getElementById("volume"), {
      type: "bar",
      data: { labels: data.volume.labels, datasets: bars(data.volume.series) },
      options: {
        maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: Object.assign({}, tooltip, {
            filter: (item) => item.raw > 0,
            footer: (items) => "Total " + items.reduce((a, i) => a + i.raw, 0),
          }),
        },
        scales: {
          x: axis({ stacked: true, grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 18, color: muted } }),
          y: axis({ stacked: true, beginAtZero: true, ticks: { precision: 0, color: muted } }),
        },
      },
    });
  }

  // Waktu keputusan per tahap: median dan p90 (satu sumbu, jam)
  if (document.getElementById("stages")) {
    const st = data.stages;
    new Chart(document.getElementById("stages"), {
      type: "bar",
      data: {
        labels: st.map((s) => s.label),
        datasets: [
          { label: "Median", data: st.map((s) => s.median && +s.median.toFixed(1)), backgroundColor: "#15876F", maxBarThickness: 14 },
          { label: "p90", data: st.map((s) => s.p90 && +s.p90.toFixed(1)), backgroundColor: "#8CCBB9", maxBarThickness: 14 },
        ],
      },
      options: {
        indexAxis: "y", maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "top", align: "end", labels: { boxWidth: 10, boxHeight: 10, color: ink } },
          tooltip: Object.assign({}, tooltip, { callbacks: { label: (c) => ` ${c.dataset.label}: ${c.raw ?? "–"} h` } }),
        },
        scales: { x: axis({ beginAtZero: true, title: { display: true, text: "hours", color: muted } }), y: axis({ grid: { display: false }, ticks: { color: ink } }) },
      },
    });
  }

  // Joiner / mover / leaver per minggu
  if (document.getElementById("jml")) {
    legend(document.getElementById("jml-legend"), data.jml.series);
    new Chart(document.getElementById("jml"), {
      type: "bar",
      data: { labels: data.jml.labels, datasets: bars(data.jml.series).map((d) => Object.assign(d, { borderWidth: 0, maxBarThickness: 10 })) },
      options: {
        maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false }, tooltip: Object.assign({}, tooltip, { filter: (i) => i.raw > 0 }) },
        scales: {
          x: axis({ grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 14, color: muted } }),
          y: axis({ beginAtZero: true, ticks: { precision: 0, color: muted } }),
        },
      },
    });
  }

  // Alert per hari (satu seri)
  if (data.alerts && document.getElementById("alerts")) {
    new Chart(document.getElementById("alerts"), {
      type: "bar",
      data: { labels: data.alerts.labels, datasets: [{ label: "Alerts", data: data.alerts.totals, backgroundColor: "#4A5866", maxBarThickness: 16 }] },
      options: {
        maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: tooltip },
        scales: {
          x: axis({ grid: { display: false }, ticks: { maxRotation: 0, autoSkipPadding: 16, color: muted } }),
          y: axis({ beginAtZero: true, ticks: { precision: 0, color: muted } }),
        },
      },
    });
  }
})();
