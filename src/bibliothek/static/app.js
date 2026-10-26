// Bibliothek — kleine Komfortfunktionen. Ohne JavaScript funktioniert alles außer der Live-Anzeige.
"use strict";

document.addEventListener("DOMContentLoaded", () => {
  // Sicherheitsabfrage für Formulare mit data-bestaetigen
  document.querySelectorAll("form[data-bestaetigen]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.bestaetigen)) event.preventDefault();
    });
  });

  // Metadaten: weitere Feldzeile anhängen
  const vorlage = document.querySelector("template[data-feldvorlage]");
  const felder = document.querySelector("[data-felder]");
  const hinzu = document.querySelector("[data-feld-hinzu]");
  if (vorlage && felder && hinzu) {
    hinzu.addEventListener("click", () => {
      const zeile = vorlage.content.firstElementChild.cloneNode(true);
      felder.appendChild(zeile);
      zeile.querySelector("input").focus();
    });
  }

  // Einzeilige Textfelder wachsen bei Bedarf mit
  document.querySelectorAll(".feldzeile textarea").forEach((ta) => {
    const passen = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + 2 + "px"; };
    ta.addEventListener("input", passen);
  });

  seitenvorschau();
  ablage();
  jobanzeige();
});

function seitenvorschau() {
  // Vorschau: welcher Versatz entsteht, und welcher Abschnitt ist betroffen
  const form = document.querySelector("[data-seitenhelfer]");
  if (!form) return;
  const scanEl = form.querySelector("[data-helfer-scan]");
  const seiteEl = form.querySelector("[data-helfer-seite]");
  const abEl = form.querySelector("[data-helfer-ab]");
  const vorschau = form.querySelector("[data-helfer-vorschau]");
  const grundtext = vorschau.textContent;
  const zeilen = document.querySelectorAll(".abschnitte-tabelle tr[data-ab]");

  const aktualisieren = () => {
    const scan = parseInt(scanEl.value, 10);
    const seite = parseInt(seiteEl.value, 10);
    const ab = parseInt(abEl.value, 10);
    zeilen.forEach((tr) => tr.classList.remove("markiert"));
    if (Number.isNaN(scan) || Number.isNaN(seite)) { vorschau.textContent = grundtext; return; }
    const v = seite - scan;
    let ziel = "";
    if (!Number.isNaN(ab)) {
      ziel = "neuer Abschnitt ab Scan " + ab;
    } else {
      let treffer = null;
      zeilen.forEach((tr) => { if (scan >= parseInt(tr.dataset.ab, 10)) treffer = tr; });
      if (treffer) {
        treffer.classList.add("markiert");
        ziel = "Abschnitt ab Scan " + treffer.dataset.ab;
      } else {
        ziel = "erster Abschnitt ab Scan " + Math.max(1, 1 - v);
      }
    }
    vorschau.textContent = "Versatz " + (v >= 0 ? "+" : "") + v + " (Druckseite = Scan " + (v >= 0 ? "+ " : "− ") + Math.abs(v) + ") für " + ziel + ".";
  };
  [scanEl, seiteEl, abEl].forEach((el) => el.addEventListener("input", aktualisieren));
}

function ablage() {
  const zone = document.querySelector("[data-ablage]");
  if (!zone) return;
  const input = zone.querySelector("input[type=file]");
  const text = zone.querySelector("[data-ablage-text]");
  const zeigen = () => {
    const n = input.files.length;
    text.textContent = n ? (n === 1 ? input.files[0].name : n + " Dateien ausgewählt") : "Dateien hierher ziehen oder klicken";
  };
  input.addEventListener("change", zeigen);
  ["dragenter", "dragover"].forEach((ev) => zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("ueber"); }));
  ["dragleave", "drop"].forEach((ev) => zone.addEventListener(ev, () => zone.classList.remove("ueber")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      zeigen();
    }
  });
}

function jobanzeige() {
  const box = document.querySelector("[data-job]");
  if (!box) return;
  const url = box.dataset.job;
  const titelUrl = box.dataset.titelUrl;
  const statusEl = box.querySelector("[data-job-status]");
  const logEl = box.querySelector("[data-job-log]");
  const fehlerEl = box.querySelector("[data-job-fehler]");
  const ergebnisEl = box.querySelector("[data-job-ergebnis]");

  const link = (key) => {
    const a = document.createElement("a");
    a.href = titelUrl.replace("__KEY__", encodeURIComponent(key));
    a.textContent = key;
    return a;
  };
  const liste = (ueberschrift, eintraege, bauen) => {
    if (!eintraege || !eintraege.length) return;
    const p = document.createElement("p");
    p.textContent = ueberschrift + " ";
    eintraege.forEach((e, i) => { if (i) p.append(", "); p.append(bauen(e)); });
    ergebnisEl.appendChild(p);
  };

  const holen = async () => {
    let job;
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      if (!r.ok) return;
      job = await r.json();
    } catch (e) {
      setTimeout(holen, 3000);
      return;
    }
    statusEl.textContent = job.status;
    statusEl.className = "jobstatus jobstatus-" + job.status;
    const unten = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8;
    logEl.textContent = job.log.join("\n");
    if (unten) logEl.scrollTop = logEl.scrollHeight;
    if (job.fehler) { fehlerEl.textContent = job.fehler; fehlerEl.hidden = false; }
    if (job.status === "fertig" || job.status === "fehler") {
      ergebnisEl.textContent = "";
      const e = job.ergebnis || {};
      liste("Aufgenommen:", e.neu, link);
      liste("Schon vorhanden:", e.doppelt, (d) => { const s = document.createElement("span"); s.append(d.datei + " → "); s.append(link(d.citekey)); return s; });
      liste("Fehlgeschlagen:", e.fehler, (d) => document.createTextNode(d.datei + " (" + d.fehler + ")"));
      if (e.neu && e.neu.length === 1 && !(e.doppelt || []).length && !(e.fehler || []).length) {
        const p = document.createElement("p");
        const a = link(e.neu[0]);
        a.textContent = "Zum neuen Titel →";
        a.className = "knopf";
        p.appendChild(a);
        ergebnisEl.appendChild(p);
      }
      return;
    }
    setTimeout(holen, 1200);
  };
  holen();
}
