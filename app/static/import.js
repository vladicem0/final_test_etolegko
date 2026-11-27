const pageSize = 50;
const campaign = document.getElementById("campaign");
const previous = document.getElementById("previous");
const next = document.getElementById("next");
const page = document.getElementById("page");
const catalogStatus = document.getElementById("catalog-status");
const retry = document.getElementById("retry");
const fetchButton = document.getElementById("fetch");
const adminLink = document.getElementById("admin-link");
const statusMessage = document.getElementById("status");
const snapshot = document.getElementById("snapshot");
const streams = document.getElementById("streams");

let offset = 0;
let hasNext = false;
let loadingPage = false;
let importing = false;
let retryOffset = 0;
let localRead;

function updateControls() {
  const busy = loadingPage || importing;
  campaign.disabled = busy || campaign.options.length < 2;
  previous.disabled = busy || offset === 0;
  next.disabled = busy || !hasNext;
  retry.disabled = busy;
  fetchButton.disabled = busy || !campaign.value;
}

function showStatus(element, message, error = false) {
  element.textContent = message;
  element.dataset.error = String(error);
}

function clearSnapshot() {
  localRead?.abort();
  snapshot.hidden = true;
  streams.replaceChildren();
  adminLink.hidden = true;
  adminLink.removeAttribute("href");
  showStatus(statusMessage, "");
}

function textElement(tag, value) {
  const element = document.createElement(tag);
  element.textContent = value;
  return element;
}

function renderStream(stream) {
  const section = document.createElement("section");
  section.className = "stream";
  section.append(textElement("h3", stream.position + ". " + stream.name));
  if (!stream.offers.length) {
    section.append(textElement("p", "Офферов нет."));
    return section;
  }

  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  for (const title of ["Оффер", "Доля", "Состояние"]) {
    const cell = textElement("th", title);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const offer of stream.offers) {
    const row = body.insertRow();
    if (offer.removed) row.className = "archived";
    row.insertCell().textContent = offer.name ? offer.name + " (#" + offer.offer_id + ")" : "#" + offer.offer_id;
    row.insertCell().textContent = offer.share + "%";
    row.insertCell().textContent = offer.removed ? "В локальном архиве" : offer.state;
  }
  section.append(table);
  return section;
}

function renderSnapshot(data) {
  document.getElementById("snapshot-title").textContent = data.name;
  streams.replaceChildren(...data.streams.map(renderStream));
  if (!data.streams.length) streams.append(textElement("p", "В кампании нет потоков."));
  snapshot.hidden = false;
  adminLink.hidden = true;
  adminLink.removeAttribute("href");
  if (data.admin_url) {
    const url = new URL(data.admin_url, window.location.origin);
    if (["http:", "https:"].includes(url.protocol)) {
      adminLink.href = url.href;
      adminLink.hidden = false;
    }
  }
}

async function loadPage(requestedOffset) {
  if (loadingPage || importing) return;
  loadingPage = true;
  retry.hidden = true;
  updateControls();
  showStatus(catalogStatus, "Загружаем список кампаний…");
  try {
    const response = await fetch("/api/campaigns?offset=" + requestedOffset + "&limit=" + pageSize);
    if (!response.ok) throw new Error("HTTP " + response.status);
    const items = await response.json();
    if (items.length || requestedOffset === 0) {
      const options = items.map(item => new Option(item.name + " (#" + item.id + ")", String(item.id)));
      campaign.replaceChildren(new Option("Выберите кампанию", ""), ...options);
      clearSnapshot();
      offset = requestedOffset;
      page.textContent = items.length ? "Страница " + (offset / pageSize + 1) : "";
    }
    hasNext = items.length === pageSize;
    const message = items.length ? "" : requestedOffset ? "Вы на последней странице." : "В Keitaro пока нет кампаний.";
    showStatus(catalogStatus, message);
  } catch (error) {
    showStatus(catalogStatus, "Не удалось загрузить список кампаний. " + error.message, true);
    retryOffset = requestedOffset;
    retry.hidden = false;
  } finally {
    loadingPage = false;
    updateControls();
  }
}

async function openSavedSnapshot() {
  clearSnapshot();
  updateControls();
  if (!campaign.value) return;
  const controller = new AbortController();
  localRead = controller;
  showStatus(statusMessage, "Открываем сохранённые потоки…");
  try {
    const response = await fetch("/api/campaigns/" + campaign.value + "/streams", { signal: controller.signal });
    if (controller.signal.aborted) return;
    if (response.status === 404) {
      showStatus(statusMessage, "Потоки этой кампании ещё не загружены. Нажмите Fetch streams from KT.");
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (controller.signal.aborted) return;
    renderSnapshot(data);
    showStatus(statusMessage, "Открыт сохранённый снимок.");
  } catch (error) {
    if (!controller.signal.aborted) showStatus(statusMessage, "Не удалось открыть сохранённые потоки. " + error.message, true);
  }
}

async function fetchStreams() {
  if (!campaign.value || loadingPage || importing) return;
  localRead?.abort();
  importing = true;
  updateControls();
  showStatus(statusMessage, "Загружаем потоки из Keitaro…");
  try {
    const response = await fetch("/api/campaigns/" + campaign.value + "/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (response.status === 409) {
      const conflict = await response.json().catch(() => null);
      if (typeof conflict?.detail === "string" && conflict.detail) throw new Error(conflict.detail);
    }
    if (!response.ok) throw new Error("HTTP " + response.status);
    renderSnapshot(await response.json());
    showStatus(statusMessage, "Потоки загружены. Снимок сохранён.");
  } catch (error) {
    showStatus(statusMessage, "Не удалось обновить потоки. " + error.message, true);
  } finally {
    importing = false;
    updateControls();
  }
}

campaign.addEventListener("change", openSavedSnapshot);
previous.addEventListener("click", () => loadPage(Math.max(offset - pageSize, 0)));
next.addEventListener("click", () => loadPage(offset + pageSize));
retry.addEventListener("click", () => loadPage(retryOffset));
fetchButton.addEventListener("click", fetchStreams);
loadPage(0);
