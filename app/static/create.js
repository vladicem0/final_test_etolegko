"use strict";

const form = document.getElementById("creator");
const inputs = document.getElementById("inputs");
const nameField = document.getElementById("name");
const geo = document.getElementById("geo");
const search = document.getElementById("search");
const offers = document.getElementById("offers");
const searchStatus = document.getElementById("search-status");
const statusLine = document.getElementById("status");
const submit = document.getElementById("submit");
const restart = document.getElementById("restart");
const link = document.getElementById("campaign-link");
const storageKey = "keitaro:pending-campaign:v1";
let pending = null;
let sending = false;
let completed = false;
let searchTimer;
let searchRequest;

function message(text, error = false) {
  statusLine.textContent = text;
  statusLine.dataset.error = String(error);
}

function preview() {
  const country = geo.value.trim().toUpperCase();
  document.getElementById("geo-preview").textContent = /^[A-Z]{2}$/.test(country)
    ? "Страна · " + country : "Выбранная страна";
  document.getElementById("offer-preview").textContent = offers.value
    ? offers.selectedOptions[0].textContent + " · 100%" : "На выбранный оффер · 100%";
}

function controls() {
  inputs.disabled = sending || !!pending || completed;
  submit.disabled = sending;
  submit.hidden = completed;
  submit.textContent = sending ? "Создаём…" : pending ? "Повторить запрос" : "Создать кампанию";
  restart.hidden = sending || (!pending && !completed);
}

function errorText(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(item => item.msg).filter(Boolean).join(". ");
  return "";
}

async function findOffers(term, controller) {
  try {
    const response = await fetch("/api/offers?" + new URLSearchParams({q: term}), {
      signal: controller.signal
    });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const matches = await response.json();
    if (controller.signal.aborted) return;
    offers.replaceChildren(new Option("Выберите оффер", ""));
    for (const offer of matches) {
      offers.add(new Option("#" + offer.id + " · " + offer.name, String(offer.id)));
    }
    searchStatus.textContent = matches.length
      ? "Найдено офферов: " + matches.length : "Совпадений нет. Измените запрос.";
  } catch (error) {
    if (!controller.signal.aborted) {
      searchStatus.textContent = "Не удалось загрузить офферы. Проверьте подключение и повторите поиск.";
    }
  }
}

search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchRequest?.abort();
  offers.replaceChildren(new Option("Сначала найдите оффер", ""));
  preview();
  const term = search.value.trim();
  searchStatus.textContent = term ? "Идёт поиск…" : "";
  if (term) {
    const controller = new AbortController();
    searchRequest = controller;
    searchTimer = setTimeout(() => findOffers(term, controller), 200);
  }
});

geo.addEventListener("input", preview);
geo.addEventListener("blur", () => { geo.value = geo.value.trim().toUpperCase(); });
offers.addEventListener("change", preview);
nameField.addEventListener("input", () => nameField.setCustomValidity(""));

function restore() {
  try {
    const saved = sessionStorage.getItem(storageKey);
    if (!saved) return;
    const operation = JSON.parse(saved);
    if (!operation.body?.request_id || !operation.body.offer_id || !operation.offerLabel) {
      throw new Error("Invalid saved operation");
    }
    pending = operation;
    nameField.value = pending.body.name;
    geo.value = pending.body.geo_code;
    offers.replaceChildren(new Option(pending.offerLabel, String(pending.body.offer_id), true, true));
    preview();
    controls();
    message("Сохранён предыдущий запрос. Повторите его, чтобы продолжить создание с теми же параметрами.");
  } catch (error) {
    inputs.disabled = true;
    submit.disabled = true;
    message("Не удалось прочитать сохранённый запрос. Проверьте хранилище вкладки и кампании в Keitaro перед новой попыткой.", true);
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  if (sending || completed) return;
  if (!pending) {
    const name = nameField.value.trim();
    if (!name) {
      nameField.setCustomValidity("Введите название кампании.");
      nameField.reportValidity();
      return;
    }
    try {
      const body = {
        request_id: crypto.randomUUID(), name,
        geo_code: geo.value.trim().toUpperCase(), offer_id: Number(offers.value)
      };
      const operation = {body, offerLabel: offers.selectedOptions[0].textContent};
      sessionStorage.setItem(storageKey, JSON.stringify(operation));
      pending = operation;
    } catch (error) {
      message("Не удалось сохранить запрос для повтора. Проверьте доступ к хранилищу вкладки.", true);
      return;
    }
  }

  sending = true;
  controls();
  message("Создаём кампанию и сверяем конфигурацию потоков…");
  try {
    const response = await fetch("/api/campaigns", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(pending.body)
    });
    const result = await response.json();
    if (!response.ok) {
      const detail = errorText(result.detail) || "HTTP " + response.status;
      if (response.status === 422) {
        sessionStorage.removeItem(storageKey);
        pending = null;
        message("Проверьте параметры. " + detail, true);
      } else {
        message("Создание не подтверждено. " + detail + " Исходный запрос сохранён для повтора.", true);
      }
      return;
    }
    if (!Number.isInteger(result.campaign_id) || !result.admin_url) {
      throw new Error("Incomplete confirmation");
    }
    const url = new URL(result.admin_url);
    if (!["http:", "https:"].includes(url.protocol)) throw new Error("Invalid campaign URL");
    link.href = url.href;
    link.hidden = false;
    message("Кампания «" + result.name + "» создана. ID " + result.campaign_id + ".");
    sessionStorage.removeItem(storageKey);
    pending = null;
    completed = true;
  } catch (error) {
    message("Не удалось подтвердить результат. Повторите сохранённый запрос с тем же идентификатором.", true);
  } finally {
    sending = false;
    controls();
  }
});

restart.addEventListener("click", () => {
  if (pending && !window.confirm("Предыдущая кампания могла уже появиться в Keitaro. Оставить этот запрос и начать создание другой кампании?")) return;
  try {
    sessionStorage.removeItem(storageKey);
  } catch (error) {
    message("Не удалось очистить данные операции. Проверьте хранилище вкладки.", true);
    return;
  }
  pending = null;
  completed = false;
  form.reset();
  nameField.setCustomValidity("");
  searchStatus.textContent = "";
  offers.replaceChildren(new Option("Сначала найдите оффер", ""));
  link.hidden = true;
  link.removeAttribute("href");
  message("");
  preview();
  controls();
  nameField.focus();
});

restore();
