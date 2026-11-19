"use strict";

const el = id => document.getElementById(id);
const state = { files: [], upload: null, listing: null, ready: false };
const MAX_TOTAL = 1_000_000_000;
let rowId = 0;

function show(id, message) {
  const element = el(id);
  element.textContent = message;
  element.hidden = !message;
}

function authenticated(signedIn) {
  el("login").hidden = signedIn;
  el("work").hidden = !signedIn;
  el("sign-out").hidden = !signedIn;
}

async function api(path, { method = "GET", json, body, offset } = {}) {
  const headers = {};
  if (json !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(json);
  } else if (body) {
    headers["Content-Type"] = "application/octet-stream";
    headers["X-Offset"] = String(offset);
  }
  const response = await fetch(path, { method, headers, body, credentials: "same-origin" });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") {
      authenticated(false);
      show("login-error", "Session expired. Enter the development password again.");
    }
    const error = new Error(result.error ?? `Request failed (${response.status}). Try again.`);
    error.status = response.status;
    throw error;
  }
  return result;
}

el("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit = event.submitter;
  const password = el("password");
  show("login-error", "");
  submit.disabled = true;
  try {
    await api("/api/login", { method: "POST", json: { password: password.value } });
    password.value = "";
    authenticated(true);
  } catch (error) {
    show("login-error", error.message);
    password.select();
  } finally {
    submit.disabled = false;
  }
});

el("sign-out").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST" });
    clearArchive();
    authenticated(false);
  } catch (error) {
    show("upload-error", error.message);
  }
});

function clearArchive() {
  Object.assign(state, { files: [], upload: null, listing: null, ready: false });
  el("files").value = "";
  setFiles([]);
  el("export").hidden = true;
  el("result").hidden = true;
  el("progress").hidden = true;
  el("reset").hidden = true;
  el("rows").replaceChildren();
  el("mapping-enabled").checked = false;
  el("mapper").hidden = true;
  show("upload-status", "");
  show("upload-error", "");
  show("mapping-status", "");
  show("export-error", "");
}

function setFiles(files) {
  state.files = files;
  const list = el("file-list");
  const rows = files.map(file => {
    const li = document.createElement("li");
    li.textContent = `${file.name} · ${(file.size / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })} MB`;
    return li;
  });
  list.replaceChildren(...rows);
  list.hidden = !files.length;
  const tooLarge = files.reduce((total, file) => total + file.size, 0) > MAX_TOTAL;
  show("upload-error", tooLarge ? "The selected files exceed 1 GB combined." : "");
  el("scan").disabled = !files.length || tooLarge;
}

async function replaceFiles(files) {
  if (state.upload) {
    try {
      await api(state.upload, { method: "DELETE" });
    } catch (error) {
      if (error.status !== 404) {
        show("upload-error", error.message);
        return;
      }
    }
  }
  clearArchive();
  setFiles(files);
}

el("files").addEventListener("change", event => replaceFiles(Array.from(event.target.files)));
el("reset").addEventListener("click", () => replaceFiles([]));
const drop = el("drop");
drop.addEventListener("dragover", event => {
  event.preventDefault();
  if (!el("files").disabled) drop.classList.add("drag");
});
drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
drop.addEventListener("drop", event => {
  event.preventDefault();
  drop.classList.remove("drag");
  if (!el("files").disabled) replaceFiles(Array.from(event.dataTransfer.files));
});

function listSequences(listing) {
  state.listing = listing;
  const available = listing.timelines.filter(item => !item.reason);
  const blocked = listing.timelines.filter(item => item.reason);
  el("timeline").replaceChildren(...available.map(item => new Option(
    `${item.name} · ${item.frames.toLocaleString()} frames`, item.id,
  )));
  el("unavailable").hidden = !blocked.length;
  show("unavailable-label", `${blocked.length.toLocaleString()} entries needing manual recovery`);
  el("unavailable-list").replaceChildren(...blocked.map(item => {
    const li = document.createElement("li");
    li.textContent = `${item.name} — ${item.reason}`;
    return li;
  }));
  el("export").hidden = false;
  el("create").disabled = !available.length;
  selectTimeline();
  return available.length;
}

function selectTimeline() {
  const item = state.listing?.timelines.find(item => item.id === el("timeline").value);
  show("sequence-info", item ?
    `${item.width} × ${item.height} · ${item.fps} fps · ${item.cuts} clips` :
    "No recoverable sequence was found in these files.");
  el("result").hidden = true;
}
el("timeline").addEventListener("change", selectTimeline);

el("scan").addEventListener("click", async () => {
  const total = state.files.reduce((size, file) => size + file.size, 0);
  show("upload-error", "");
  el("scan").disabled = true;
  el("files").disabled = true;
  el("reset").disabled = true;
  try {
    if (!state.ready) {
      const request = { files: state.files.map(({ name, size }) => ({ name, size })) };
      const { id, chunk } = await api("/api/upload", { method: "POST", json: request });
      state.upload = `/api/upload/${id}`;
      const progress = el("progress");
      progress.value = 0;
      progress.hidden = false;
      let sent = 0;
      for (const [index, file] of state.files.entries()) {
        show("upload-status", `Uploading ${file.name}…`);
        for (let start = 0; start < file.size; start += chunk) {
          const payload = file.slice(start, start + chunk);
          await api(`${state.upload}/${index}`, { method: "POST", body: payload, offset: start });
          sent += payload.size;
          progress.value = 100 * sent / total;
        }
      }
      state.ready = true;
    }
    show("upload-status", "Reading sequences and media paths…");
    const listing = await api(`${state.upload}/inspect`, { method: "POST" });
    const number = listSequences(listing);
    show("upload-status", number ? `${number} recoverable ${number === 1 ? "sequence" : "sequences"}.` : "Archive read.");
    el("scan").disabled = true;
  } catch (error) {
    if (state.upload && (error.status !== 503 || !state.ready)) {
      const upload = state.upload;
      state.upload = null;
      state.ready = false;
      await api(upload, { method: "DELETE" }).catch(() => {});
    }
    show("upload-status", "");
    show("upload-error", error.message);
    el("scan").disabled = !state.files.length;
  } finally {
    el("progress").hidden = true;
    el("files").disabled = false;
    el("reset").disabled = false;
    el("reset").hidden = !state.upload;
  }
});

function mapRow(source = "") {
  const row = document.createElement("div");
  row.className = "map-row";
  const id = ++rowId;
  for (const [type, title, placeholder] of [
    ["source", "Source base path", "/Volumes/oldstorage/project/"],
    ["target", "Target base path", "/Volumes/newstorage/newproject/"],
  ]) {
    const group = document.createElement("div");
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.id = `${type}-${id}`;
    label.htmlFor = input.id;
    label.textContent = title;
    input.dataset.field = type;
    input.placeholder = placeholder;
    input.required = true;
    input.maxLength = 4096;
    input.setAttribute("spellcheck", "false");
    input.autocomplete = "off";
    if (type === "source") input.value = source;
    group.append(label, input);
    row.append(group);
  }
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "quiet";
  remove.textContent = "Remove";
  remove.setAttribute("aria-label", source ? `Remove mapping for ${source}` : "Remove mapping");
  remove.addEventListener("click", () => {
    row.remove();
    el("result").hidden = true;
    el("add-map").focus();
  });
  row.append(remove);
  el("rows").append(row);
  return row;
}

el("mapping-enabled").addEventListener("change", event => {
  el("mapper").hidden = !event.target.checked;
  for (const input of el("rows").querySelectorAll("input")) input.disabled = !event.target.checked;
});
el("add-map").addEventListener("click", () => mapRow().querySelector("input").focus());
el("add-bases").addEventListener("click", () => {
  const canonical = value => value.replace(/\/+$/, "") + "/";
  const have = new Set([...document.querySelectorAll('[data-field="source"]')].map(item => canonical(item.value.trim())));
  const add = state.listing.bases.filter(base => !have.has(base));
  add.forEach(base => mapRow(base));
  show("mapping-status", add.length ? `${add.length} source ${add.length === 1 ? "base path" : "base paths"} added.` : "All suggested base paths are already listed.");
});

function writeReport(result) {
  el("download").href = result.url;
  show("result-meta", `${result.timeline} · ${result.clips} clips`);
  show("report-summary", result.rows.length ?
    `${result.rows.length} clips have review notes or remapped paths. Review notes also appear as timeline markers in Resolve.` :
    "No review notes for this export.");
  el("report-table").hidden = !result.rows.length;
  el("report").replaceChildren(...result.rows.map(entry => {
    const tr = document.createElement("tr");
    for (const text of [entry.frame + result.start, entry.name]) {
      const cell = document.createElement("td");
      cell.textContent = text;
      tr.append(cell);
    }
    const review = document.createElement("td");
    const list = document.createElement("ul");
    for (const issue of entry.issues) {
      const item = document.createElement("li");
      item.textContent = issue;
      list.append(item);
    }
    if (list.childNodes.length) review.append(list);
    if (entry.source !== entry.destination) {
      for (const [label, path] of [["From", entry.source], ["To", entry.destination]]) {
        const line = document.createElement("code");
        line.textContent = `${label} ${path}`;
        review.append(line);
      }
    }
    tr.append(review);
    return tr;
  }));
  el("result").hidden = false;
  el("result-heading").setAttribute("tabindex", "-1");
  el("result-heading").focus();
}

el("export").addEventListener("input", () => { el("result").hidden = true; });
el("export").addEventListener("submit", async event => {
  event.preventDefault();
  el("result").hidden = true;
  show("export-error", "");
  el("create").disabled = true;
  try {
    const remap = el("mapping-enabled").checked;
    const mappings = remap ? [...el("rows").children].map(row => Object.fromEntries(
      [...row.querySelectorAll("input")].map(input => [input.dataset.field, input.value.trim()]),
    )) : [];
    const json = { timeline: el("timeline").value, scale: el("scale").value, remap, mappings };
    writeReport(await api(`${state.upload}/export`, { method: "POST", json }));
  } catch (error) {
    show("export-error", error.message);
  } finally {
    el("create").disabled = false;
  }
});
el("print").addEventListener("click", () => window.print());

api("/api/session").then(result => authenticated(result.authenticated)).catch(error => {
  authenticated(false);
  show("login-error", error.message);
});
