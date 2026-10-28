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
  schnellAufnahme();
  nasStatus();
  nasLogin();
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
      if (e.meldung) { const m = document.createElement("p"); m.className = "meldung ok"; m.textContent = e.meldung; ergebnisEl.appendChild(m); }
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

// --- Literatur hinzufügen: Knopf in der Kopfzeile und Ablegen irgendwo auf der Seite ---------
function toast(inhalt, art) {
  const box = document.querySelector("[data-toasts]");
  const t = document.createElement("div");
  t.className = "toast " + (art || "");
  t.setAttribute("role", "status");
  const zu = document.createElement("button");
  zu.type = "button";
  zu.className = "toast-zu";
  zu.setAttribute("aria-label", "Meldung schließen");
  zu.textContent = "×";
  zu.addEventListener("click", () => t.remove());
  const text = document.createElement("div");
  text.className = "toast-text";
  t.append(text, zu);
  box.appendChild(t);
  const setzen = (neu, neueArt) => {
    text.textContent = "";
    (Array.isArray(neu) ? neu : [neu]).forEach((x) => text.append(x));
    if (neueArt !== undefined) t.className = "toast " + neueArt;
  };
  setzen(inhalt);
  return { el: t, setzen, schliessen: (ms) => setTimeout(() => t.remove(), ms) };
}

function schnellAufnahme() {
  const form = document.querySelector("form[data-hinzufuegen]");
  if (!form) return;
  const input = form.querySelector("[data-hinzufuegen-datei]");
  const csrf = form.querySelector("input[name=csrf]").value;
  const titelUrl = document.querySelector("[data-toasts]").dataset.titelUrl;
  const erlaubt = (input.getAttribute("accept") || "").split(",");

  const link = (key, text) => {
    const a = document.createElement("a");
    a.href = titelUrl.replace("__KEY__", encodeURIComponent(key));
    a.textContent = text || key;
    return a;
  };

  const verfolgen = (antwort, t) => {
    const jobLink = document.createElement("a");
    jobLink.href = antwort.url;
    jobLink.textContent = "Fortschritt";
    t.setzen([`Nehme ${antwort.anzahl} Datei(en) auf … `, jobLink], "laeuft");
    const holen = async () => {
      let job;
      try {
        const r = await fetch(antwort.api, { headers: { Accept: "application/json" } });
        job = await r.json();
      } catch (e) { setTimeout(holen, 3000); return; }
      if (job.status !== "fertig" && job.status !== "fehler") {
        const letzte = job.log.length ? job.log[job.log.length - 1].replace(/^\S+\s+/, "") : "";
        t.setzen([`Nehme ${antwort.anzahl} Datei(en) auf … `, letzte ? letzte + " · " : "", jobLink], "laeuft");
        setTimeout(holen, 1500);
        return;
      }
      if (job.status === "fehler") { t.setzen(["Aufnahme fehlgeschlagen: " + job.fehler + " · ", jobLink], "fehler"); return; }
      const e = job.ergebnis || {};
      const teile = [];
      if ((e.neu || []).length) {
        teile.push("Aufgenommen: ");
        e.neu.forEach((k, i) => { if (i) teile.push(", "); teile.push(link(k)); });
        teile.push(". ");
      }
      (e.doppelt || []).forEach((d) => { teile.push(`„${d.datei}“ gibt es schon: `); teile.push(link(d.citekey)); teile.push(". "); });
      (e.fehler || []).forEach((d) => teile.push(`„${d.datei}“: ${d.fehler}. `));
      if (!teile.length) teile.push("Nichts aufgenommen.");
      t.setzen(teile, (e.fehler || []).length ? "fehler" : ((e.doppelt || []).length && !(e.neu || []).length ? "hinweis" : "ok"));
      if (!(e.fehler || []).length) t.schliessen(20000);
    };
    holen();
  };

  const hochladen = async (dateien) => {
    const liste = Array.from(dateien).filter((f) => {
      const endung = "." + (f.name.split(".").pop() || "").toLowerCase();
      return erlaubt.includes(endung);
    });
    const abgelehnt = dateien.length - liste.length;
    if (abgelehnt) toast(`${abgelehnt} Datei(en) übersprungen — Format wird nicht unterstützt.`, "hinweis").schliessen(8000);
    if (!liste.length) return;
    const daten = new FormData();
    liste.forEach((f) => daten.append("dateien", f, f.name));
    const t = toast(`Lade ${liste.length} Datei(en) hoch …`, "laeuft");
    try {
      const r = await fetch(form.dataset.hinzufuegen, { method: "POST", body: daten, headers: { "X-CSRF-Token": csrf, Accept: "application/json" } });
      const antwort = await r.json();
      if (!r.ok) { t.setzen("Hochladen fehlgeschlagen: " + (antwort.fehler || r.status), "fehler"); return; }
      verfolgen(antwort, t);
    } catch (e) {
      t.setzen("Hochladen fehlgeschlagen: " + e, "fehler");
    }
  };

  input.addEventListener("change", () => {
    if (input.files.length) hochladen(input.files);
    input.value = "";
  });

  // Dateien irgendwo auf die Seite ziehen (außer auf die eigene Ablagefläche der Seite „Aufnehmen“)
  const schicht = document.querySelector("[data-ablegen]");
  let tiefe = 0;
  const istDateien = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  const eigeneZone = (e) => e.target.closest && e.target.closest("[data-ablage]");
  window.addEventListener("dragenter", (e) => { if (!istDateien(e) || eigeneZone(e)) return; tiefe++; schicht.hidden = false; });
  window.addEventListener("dragleave", (e) => { if (!istDateien(e)) return; tiefe = Math.max(0, tiefe - 1); if (!tiefe) schicht.hidden = true; });
  window.addEventListener("dragover", (e) => { if (istDateien(e) && !eigeneZone(e)) e.preventDefault(); });
  window.addEventListener("drop", (e) => {
    if (!istDateien(e) || eigeneZone(e)) return;
    e.preventDefault();
    tiefe = 0;
    schicht.hidden = true;
    hochladen(e.dataTransfer.files);
  });
}

// --- Statusanzeige Nextcloud ---------------------------------------------------------------------
function nasStatus() {
  const el = document.querySelector("[data-nas-status]");
  if (!el) return;
  const text = el.querySelector("[data-nas-text]");
  const holen = async () => {
    let laeuft = false;
    try {
      const r = await fetch(el.dataset.nasStatus, { headers: { Accept: "application/json" } });
      const s = await r.json();
      const a = (s.abgleich || {}).anzeige;
      if (a) {
        text.textContent = a.text;
        el.className = "nas-status nas-" + a.klasse;
        laeuft = a.klasse === "laeuft";
      }
    } catch (e) { /* Server beendet */ }
    setTimeout(holen, laeuft ? 3000 : 30000);
  };
  setTimeout(holen, el.classList.contains("nas-laeuft") ? 3000 : 30000);
}

// --- Anmeldung bei der Nextcloud: warten, bis im Browser bestätigt ---------------------------------
function nasLogin() {
  const box = document.querySelector("[data-nas-login]");
  if (!box || box.dataset.loginStatus !== "wartet") return;
  const holen = async () => {
    try {
      const r = await fetch(box.dataset.nasLogin, { headers: { Accept: "application/json" } });
      const s = await r.json();
      if (s.status !== "wartet") { window.location.reload(); return; }
    } catch (e) { /* weiter versuchen */ }
    setTimeout(holen, 2000);
  };
  setTimeout(holen, 2000);
}
