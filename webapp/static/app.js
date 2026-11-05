// autospoti · comportamiento del navegador (sin dependencias propias; HTMX y Vega vienen del CDN).
"use strict";

// Campos del export que se envían al servidor (ni IPs ni ubicación).
const IMPORT_FIELDS = [
  "ts", "ms_played", "spotify_track_uri", "master_metadata_track_name",
  "master_metadata_album_artist_name", "master_metadata_album_album_name",
  "skipped", "reason_end", "shuffle", "platform", "trackName",
];

const VEGA_CONFIG = {
  background: null,
  font: "Inter, system-ui, sans-serif",
  axis: {
    labelColor: "#a9a8b6", titleColor: "#a9a8b6", labelFontSize: 11, titleFontWeight: 500,
    gridColor: "rgba(255,255,255,0.06)", domainColor: "rgba(255,255,255,0.15)",
    tickColor: "rgba(255,255,255,0.15)",
  },
  view: { stroke: null },
};

function renderCharts(root) {
  const holder = root.querySelector("#chart-specs");
  if (!holder || typeof vegaEmbed === "undefined") return;
  const specs = JSON.parse(holder.textContent || "{}");
  root.querySelectorAll(".chart[data-spec]").forEach((el) => {
    const spec = specs[el.dataset.spec];
    if (!spec) return;
    spec.width = "container";
    spec.autosize = { type: "fit-x", contains: "padding" };
    vegaEmbed(el, spec, { actions: false, config: VEGA_CONFIG, tooltip: { theme: "dark" }, renderer: "svg" })
      .catch((err) => { el.textContent = "No se pudo dibujar el gráfico."; console.error(err); });
  });
}

// Formularios: confirmación, estado "ocupado" y envío automático.
document.addEventListener("submit", (ev) => {
  const form = ev.target;
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    ev.preventDefault();
    return;
  }
  if (form.dataset.busy) {
    const button = form.querySelector("button");
    if (button) {
      button.disabled = true;
      button.dataset.label = button.innerHTML;
      button.textContent = form.dataset.busy;
    }
  }
});

document.addEventListener("change", (ev) => {
  const form = ev.target.closest("form[data-autosubmit]");
  if (form) form.submit();
  // Top de Spotify: el nombre sigue al periodo elegido.
  if (ev.target.matches("input[data-name]")) {
    const target = document.querySelector("[data-name-target]");
    if (target) target.value = ev.target.dataset.name;
  }
});

// Al volver con "atrás", que los botones no se queden en "Actualizando…".
window.addEventListener("pageshow", () => {
  document.querySelectorAll("button[data-label]").forEach((b) => {
    b.disabled = false;
    b.innerHTML = b.dataset.label;
  });
});

// ---------------------------------------------------------------- importar

function pick(entry) {
  const out = {};
  for (const key of IMPORT_FIELDS) if (entry && key in entry) out[key] = entry[key];
  return out;
}

async function importFiles(files, batchSize, ui) {
  const totals = { entries: 0, inserted: 0, duplicates: 0, no_uri: 0, not_track: 0, legacy_format: 0, invalid: 0 };
  const parsed = [];
  for (const file of files) {
    try {
      const data = JSON.parse(await file.text());
      if (!Array.isArray(data)) throw new Error("no es una lista de reproducciones");
      parsed.push({ name: file.name, data });
    } catch (err) {
      ui.status(`${file.name}: no se pudo leer (${err.message}).`);
      return;
    }
  }
  const total = parsed.reduce((n, f) => n + f.data.length, 0);
  let done = 0;
  for (const file of parsed) {
    for (let i = 0; i < file.data.length; i += batchSize) {
      const entries = file.data.slice(i, i + batchSize).map(pick);
      const res = await fetch("/api/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ entries }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        ui.status(`Error importando ${file.name}: ${body.error || res.status}`);
        return;
      }
      for (const key of Object.keys(totals)) totals[key] += body[key] || 0;
      done += entries.length;
      ui.progress(done / total);
      ui.status(`${file.name}: ${done.toLocaleString("es-ES")} de ${total.toLocaleString("es-ES")} procesadas…`);
    }
  }
  const skipped = totals.no_uri + totals.not_track + totals.invalid;
  let msg = `Listo: ${totals.inserted.toLocaleString("es-ES")} escuchas nuevas, ` +
            `${totals.duplicates.toLocaleString("es-ES")} ya estaban` +
            (skipped ? `, ${skipped.toLocaleString("es-ES")} omitidas (podcasts, archivos locales…)` : "") + ".";
  if (totals.legacy_format) {
    msg += " Algunos archivos tienen el formato antiguo (sin identificadores de canción) y no sirven: " +
           "pide el «Extended streaming history».";
  }
  ui.status(msg);
}

function setupImporter() {
  const box = document.getElementById("importer");
  if (!box || box.dataset.ready) return;
  box.dataset.ready = "1";
  const input = document.getElementById("import-files");
  const go = document.getElementById("import-go");
  const bar = document.getElementById("import-bar");
  const statusEl = document.getElementById("import-status");
  const ui = {
    status: (t) => { statusEl.textContent = t; },
    progress: (f) => { bar.parentElement.hidden = false; bar.style.width = `${Math.round(f * 100)}%`; },
  };
  input.addEventListener("change", () => {
    go.disabled = input.files.length === 0;
    ui.status(input.files.length ? `${input.files.length} archivo(s) elegido(s).` : "");
  });
  go.addEventListener("click", async () => {
    go.disabled = true;
    input.disabled = true;
    try {
      await importFiles(Array.from(input.files), Number(box.dataset.batch) || 2000, ui);
    } catch (err) {
      ui.status(`Error de conexión: ${err.message}. Puedes volver a intentarlo; lo ya importado no se duplica.`);
    } finally {
      input.disabled = false;
      go.disabled = false;
    }
  });
}

function init(root) {
  renderCharts(root);
  setupImporter();
}

document.addEventListener("DOMContentLoaded", () => init(document));
document.addEventListener("htmx:afterSettle", (ev) => init(ev.detail.elt || document));
