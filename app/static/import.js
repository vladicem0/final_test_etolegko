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
const discardDialog = document.getElementById("discard-dialog");
let currentSnapshot;
let pendingDiscard;

let offset = 0;
let hasNext = false;
let loadingPage = false;
let importing = false;
let retryOffset = 0;
let localRead;

function updateControls() {
  const busy = loadingPage || importing;
  streams.disabled = busy;
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
  currentSnapshot = null;
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

function actionButton(label, handler) {
  const button = textElement("button", label);
  button.type = "button";
  button.className = "secondary";
  button.addEventListener("click", handler);
  return button;
}

function offerActions(stream, offer) {
  const actions = document.createElement("div");
  actions.className = "offer-actions";
  const edit = (action, fields = {}) => editStream(stream, {action, offer_id: offer.offer_id, ...fields});
  if (offer.removed) {
    actions.append(actionButton("Bring back", () => edit("restore")));
    return actions;
  }
  if (offer.state === "active") {
    if (offer.pinned) {
      actions.append(actionButton("Unpin", () => edit("unpin")));
    } else {
      const share = document.createElement("input");
      share.type = "number";
      share.className = "pin-value";
      share.min = "1";
      share.max = "100";
      share.step = "1";
      share.required = true;
      share.value = String(offer.share);
      share.setAttribute("aria-label", "Доля для закрепления оффера #" + offer.offer_id + ", %");
      actions.append(share, actionButton("Pin", () => {
        if (share.reportValidity()) edit("pin", {share: share.valueAsNumber});
      }));
    }
  }
  actions.append(actionButton("Remove", () => edit("remove")));
  return actions;
}

function offerSearch(stream) {
  const box = document.createElement("div");
  box.className = "offer-add";
  const searchLabel = textElement("label", "Найти оффер");
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "ID или название";
  searchLabel.append(search);
  const selectLabel = textElement("label", "Результаты поиска");
  const matches = document.createElement("select");
  matches.add(new Option("Введите запрос", ""));
  selectLabel.append(matches);
  const add = actionButton("Add", () => {
    if (matches.value) editStream(stream, {action: "add", offer_id: Number(matches.value)});
  });
  add.disabled = true;
  matches.addEventListener("change", () => { add.disabled = !matches.value; });
  const inStream = new Set(stream.offers.map(offer => offer.offer_id));
  let timer, activeRequest;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    activeRequest?.abort();
    add.disabled = true;
    const query = search.value.trim();
    matches.replaceChildren(new Option(query ? "Идёт поиск…" : "Введите запрос", ""));
    if (!query) return;
    const controller = new AbortController();
    activeRequest = controller;
    timer = setTimeout(async () => {
      if (!box.isConnected) return;
      try {
        const response = await fetch("/api/offers?" + new URLSearchParams({q: query}), {signal: controller.signal});
        if (!response.ok) throw new Error("HTTP " + response.status);
        const results = (await response.json()).filter(item => !inStream.has(item.id));
        if (controller.signal.aborted || !box.isConnected) return;
        matches.replaceChildren(new Option(results.length ? "Выберите оффер" : "Нет доступных совпадений", ""),
          ...results.map(item => new Option("#" + item.id + " · " + item.name, String(item.id))));
      } catch (error) {
        if (!controller.signal.aborted && box.isConnected) {
          matches.replaceChildren(new Option("Ошибка поиска: " + error.message, ""));
        }
      }
    }, 200);
  });
  box.append(searchLabel, selectLabel, add);
  return box;
}

function renderStream(stream) {
  const section = document.createElement("section");
  section.className = "stream";
  const heading = document.createElement("div");
  heading.className = "stream-heading";
  heading.append(textElement("h3", stream.position + ". " + stream.name));
  if (stream.has_draft) {
    const indicator = textElement("p", "Есть неопубликованные изменения");
    indicator.className = "dirty";
    heading.append(indicator, actionButton("Cancel", () => editStream(stream, {action: "cancel"})));
  }
  section.append(heading);
  if (stream.editable) section.append(offerSearch(stream));
  if (!stream.offers.length) {
    section.append(textElement("p", "Офферов нет."));
    return section;
  }

  const table = document.createElement("table");
  const header = table.createTHead().insertRow();
  const columns = ["Оффер", "Доля", "Состояние"];
  if (stream.editable) columns.push("Действия");
  for (const title of columns) {
    const cell = textElement("th", title);
    cell.scope = "col";
    header.append(cell);
  }
  const body = table.createTBody();
  for (const offer of stream.offers) {
    const row = body.insertRow();
    if (offer.removed) row.className = "archived";
    row.insertCell().textContent = offer.name ? offer.name + " (#" + offer.offer_id + ")" : "#" + offer.offer_id;
    row.insertCell().textContent = offer.share + "%" + (offer.pinned ? " · закреплена" : "");
    row.insertCell().textContent = offer.removed ? "В локальном архиве" : offer.state;
    if (stream.editable) row.insertCell().append(offerActions(stream, offer));
  }
  const scroll = document.createElement("div");
  scroll.className = "table-scroll";
  scroll.append(table);
  section.append(scroll);
  return section;
}

function renderSnapshot(data) {
  currentSnapshot = data;
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

function saveSelection() {
  const url = new URL(window.location.href);
  if (campaign.value) url.searchParams.set("campaign", campaign.value);
  else url.searchParams.delete("campaign");
  if (offset) url.searchParams.set("offset", String(offset));
  else url.searchParams.delete("offset");
  history.replaceState(null, "", url);
}

async function loadPage(requestedOffset, restoreId = "") {
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
      if (restoreId) {
        if (!items.some(item => String(item.id) === restoreId)) {
          campaign.add(new Option("Кампания #" + restoreId, restoreId));
        }
        campaign.value = restoreId;
      } else clearSnapshot();
      offset = requestedOffset;
      saveSelection();
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
  saveSelection();
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

async function postChange(path, fields, progress, done) {
  if (!campaign.value || loadingPage || importing) return;
  localRead?.abort();
  importing = true;
  updateControls();
  showStatus(statusMessage, progress);
  const campaignUrl = "/api/campaigns/" + campaign.value;
  try {
    const response = await fetch(campaignUrl + path, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(fields),
    });
    const result = await response.json();
    if (!response.ok) {
      let reason = typeof result.detail === "string" ? result.detail : "HTTP " + response.status;
      if (response.status === 409) {
        // Another tab may have created or changed a draft since this screen was loaded.
        try {
          const local = await fetch(campaignUrl + "/streams");
          if (local.ok) renderSnapshot(await local.json());
          else if (local.status === 404) clearSnapshot();
          else throw new Error("HTTP " + local.status);
        } catch (error) {
          reason += " Не удалось перечитать локальный снимок. " + error.message;
        }
      }
      throw new Error(reason);
    }
    renderSnapshot(result);
    showStatus(statusMessage, done);
  } catch (error) {
    showStatus(statusMessage, error.message, true);
  } finally {
    importing = false;
    updateControls();
  }
}

function editStream(stream, fields) {
  const done = fields.action === "cancel" ? "Черновик потока отменён." : "Изменения сохранены локально.";
  return postChange("/streams/" + stream.id + "/draft", {...fields, expected_revision: stream.revision},
    "Сохраняем изменения потока…", done);
}

function submitFetch(fields) {
  return postChange("/fetch", fields, "Загружаем потоки из Keitaro…", "Потоки загружены. Снимок сохранён.");
}

function fetchStreams() {
  if (!campaign.value || loadingPage || importing) return;
  const drafts = currentSnapshot?.streams.filter(stream => stream.has_draft) ?? [];
  if (!drafts.length) return submitFetch({});
  pendingDiscard = Object.fromEntries(drafts.map(stream => [stream.id, stream.revision]));
  document.getElementById("discard-info").textContent =
    "После успешной загрузки будут удалены локальные изменения в потоках: " +
    drafts.map(stream => stream.name + " (#" + stream.id + ")").join(", ") + ".";
  discardDialog.returnValue = "keep";
  discardDialog.showModal();
}

discardDialog.addEventListener("close", () => {
  const revisions = pendingDiscard;
  pendingDiscard = null;
  if (discardDialog.returnValue === "discard" && revisions) submitFetch({discard_drafts: revisions});
});

campaign.addEventListener("change", openSavedSnapshot);
previous.addEventListener("click", () => loadPage(Math.max(offset - pageSize, 0)));
next.addEventListener("click", () => loadPage(offset + pageSize));
retry.addEventListener("click", () => loadPage(retryOffset, retryOffset === offset ? campaign.value : ""));
fetchButton.addEventListener("click", fetchStreams);
const savedParams = new URLSearchParams(window.location.search);
const savedId = savedParams.get("campaign") || "";
const restoreId = /^[1-9]\d*$/.test(savedId) ? savedId : "";
const pageOffset = Number(savedParams.get("offset"));
if (Number.isSafeInteger(pageOffset) && pageOffset >= 0 && pageOffset % pageSize === 0) offset = pageOffset;
if (restoreId) {
  campaign.add(new Option("Кампания #" + restoreId, restoreId, true, true));
  openSavedSnapshot();
}
loadPage(offset, restoreId);
