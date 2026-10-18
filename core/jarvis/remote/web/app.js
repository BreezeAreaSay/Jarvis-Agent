/* Пульт Jarvis в браузере телефона. Без сборки и библиотек.
 *
 * Токен приходит один раз в ссылке (?t=…), страница кладёт его в localStorage и убирает из адресной строки.
 * Все запросы — /remote/api/* с заголовком X-Remote-Token. Состояние (громкость, что играет) — опрос раз в 3 с,
 * пока страница на экране.
 */
(() => {
  "use strict";

  const TOKEN_KEY = "jarvis.remote.token";
  const POLL_MS = 3000;
  const $ = (id) => document.getElementById(id);

  // --- токен --------------------------------------------------------------------------------------
  const params = new URLSearchParams(location.search);
  let token = params.get("t") || "";
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else token = localStorage.getItem(TOKEN_KEY) || "";
  } catch (_) {
    /* приватный режим — живём с токеном из ссылки */
  }
  if (params.has("t")) history.replaceState(null, "", location.pathname);

  // --- запросы ------------------------------------------------------------------------------------
  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }

  async function api(path, { method, json, blob } = {}) {
    const headers = { "X-Remote-Token": token };
    const init = { method: method || (json !== undefined ? "POST" : "GET"), headers, cache: "no-store" };
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(json);
    }
    let res;
    try {
      res = await fetch("/remote/api/" + path, init);
    } catch (_) {
      setLink(false);
      throw new ApiError("Нет связи с компьютером", 0);
    }
    if (res.status === 401) {
      showDenied();
      throw new ApiError("Нет доступа", 401);
    }
    if (!res.ok) {
      let detail = "Ошибка " + res.status;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) {
        /* ответ не JSON */
      }
      throw new ApiError(detail, res.status);
    }
    setLink(true);
    return blob ? res.blob() : res.json();
  }

  function showDenied() {
    $("denied").classList.remove("hidden");
    $("app").classList.add("hidden");
    stopPolling();
  }

  // --- мелочи интерфейса --------------------------------------------------------------------------
  let toastTimer = 0;
  function toast(text, isError = false) {
    const el = $("toast");
    el.textContent = text;
    el.classList.toggle("error", isError);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 3600 : 2200);
  }

  function buzz() {
    if (navigator.vibrate) navigator.vibrate(8);
  }

  function setLink(online) {
    const link = $("link");
    link.classList.toggle("online", online);
    link.classList.toggle("offline", !online);
    $("link-text").textContent = online ? "На связи" : "Нет связи";
  }

  function useIcon(svg, name) {
    svg.querySelector("use").setAttribute("href", "#i-" + name);
  }

  function makeIcon(name) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    const use = document.createElementNS(ns, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  async function busy(button, work) {
    if (button) button.classList.add("busy");
    try {
      return await work();
    } finally {
      if (button) button.classList.remove("busy");
    }
  }

  function done(result, fallback) {
    if (!result) return;
    const title = result.title || fallback || "Готово";
    const text = result.message ? title + " · " + result.message : title;
    toast(text, result.ok === false);
  }

  // --- подтверждение ------------------------------------------------------------------------------
  function confirmSheet(title, text, okText) {
    return new Promise((resolve) => {
      const sheet = $("sheet");
      $("toast").classList.add("hidden"); // тост не должен закрывать кнопки подтверждения
      $("sheet-title").textContent = title;
      $("sheet-text").textContent = text;
      $("sheet-ok").textContent = okText;
      sheet.classList.remove("hidden");
      const finish = (answer) => {
        sheet.classList.add("hidden");
        $("sheet-ok").onclick = null;
        $("sheet-cancel").onclick = null;
        sheet.onclick = null;
        resolve(answer);
      };
      $("sheet-ok").onclick = () => finish(true);
      $("sheet-cancel").onclick = () => finish(false);
      sheet.onclick = (event) => {
        if (event.target === sheet) finish(false);
      };
    });
  }

  // --- состояние ----------------------------------------------------------------------------------
  let volumeTouchedAt = 0;
  let coverId = "";
  let coverUrl = "";

  function renderVolume(volume, muted) {
    if (Date.now() - volumeTouchedAt < 1500) return; // человек двигает ползунок — не мешаем
    const slider = $("volume");
    slider.value = String(volume);
    slider.style.setProperty("--fill", volume + "%");
    $("volume-value").textContent = muted ? "Выкл." : volume + "%";
    $("mute-label").textContent = muted ? "Со звуком" : "Без звука";
    useIcon($("mute-icon"), muted ? "speaker" : "mute");
  }

  function renderMedia(media) {
    const has = Boolean(media && media.title);
    $("track-title").textContent = has ? media.title : "Ничего не играет";
    const sub = has ? [media.artist, media.app].filter(Boolean).join(" · ") : "Включите музыку на компьютере";
    $("track-sub").textContent = sub || " ";
    const playing = has && media.status === "playing";
    $("pp-label").textContent = playing ? "Пауза" : "Играть";
    useIcon($("pp-icon"), playing ? "pause" : "play");
    loadCover(has ? media.thumb : "");
  }

  async function loadCover(id) {
    if (id === coverId) return;
    coverId = id;
    const img = $("cover-img");
    if (!id) {
      img.classList.add("hidden");
      return;
    }
    try {
      const blob = await api("media/thumbnail", { blob: true });
      if (coverId !== id) return;
      if (coverUrl) URL.revokeObjectURL(coverUrl);
      coverUrl = URL.createObjectURL(blob);
      img.src = coverUrl;
      img.classList.remove("hidden");
    } catch (_) {
      img.classList.add("hidden");
    }
  }

  async function refresh() {
    try {
      const state = await api("state");
      $("pc").textContent = state.pc_name ? state.pc_name + " · подключено" : "Подключено";
      if (state.assistant) $("title").textContent = state.assistant;
      renderVolume(state.volume, state.muted);
      renderMedia(state.media);
      const voice = state.voice && state.voice.state;
      $("orb").classList.toggle("active", voice === "listening" || voice === "speaking" || voice === "processing");
    } catch (error) {
      if (error.status !== 401) $("pc").textContent = "Компьютер не отвечает";
    }
  }

  let pollTimer = 0;
  function startPolling() {
    stopPolling();
    refresh();
    pollTimer = setInterval(refresh, POLL_MS);
  }

  function stopPolling() {
    clearInterval(pollTimer);
    pollTimer = 0;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPolling();
    else if (!$("app").classList.contains("hidden")) startPolling();
  });

  // --- медиа --------------------------------------------------------------------------------------
  document.querySelectorAll("[data-media]").forEach((button) => {
    button.addEventListener("click", () => {
      buzz();
      busy(button, async () => {
        try {
          const result = await api("media/" + button.dataset.media, { json: {} });
          if (typeof result.volume === "number") {
            volumeTouchedAt = 0;
            renderVolume(result.volume, result.muted);
          }
          done(result);
          setTimeout(refresh, 450);
        } catch (error) {
          if (error.status !== 401) toast(error.message, true);
        }
      });
    });
  });

  let volumeTimer = 0;
  $("volume").addEventListener("input", (event) => {
    const level = Number(event.target.value);
    volumeTouchedAt = Date.now();
    event.target.style.setProperty("--fill", level + "%");
    $("volume-value").textContent = level + "%";
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(async () => {
      try {
        await api("volume", { json: { level } });
        volumeTouchedAt = Date.now();
      } catch (error) {
        if (error.status !== 401) toast(error.message, true);
      }
    }, 120);
  });

  // --- система ------------------------------------------------------------------------------------
  let shotUrl = "";

  async function loadScreenshot() {
    const img = $("viewer-img");
    $("viewer-spinner").classList.remove("hidden");
    try {
      const blob = await api("screenshot", { blob: true });
      if (shotUrl) URL.revokeObjectURL(shotUrl);
      shotUrl = URL.createObjectURL(blob);
      img.src = shotUrl;
    } catch (error) {
      if (error.status !== 401) toast(error.message, true);
      if (!shotUrl) $("viewer").classList.add("hidden");
    } finally {
      $("viewer-spinner").classList.add("hidden");
    }
  }

  function closeViewer() {
    $("viewer").classList.add("hidden");
    $("viewer-img").removeAttribute("src");
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = "";
  }

  $("viewer-close").addEventListener("click", closeViewer);
  $("viewer-refresh").addEventListener("click", loadScreenshot);

  const SYSTEM = {
    minimize_all: () => api("window/minimize_all", { json: {} }),
    lock: () => api("power/lock", { json: {} }),
    sleep: () => api("power/sleep", { json: {} }),
    shutdown: async () => {
      const ok = await confirmSheet(
        "Выключить компьютер?",
        "Несохранённые данные пропадут. Включить его с телефона не получится.",
        "Выключить"
      );
      return ok ? api("power/shutdown", { json: {} }) : null;
    },
  };

  document.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => {
      buzz();
      const action = button.dataset.action;
      if (action === "screenshot") {
        $("toast").classList.add("hidden");
        $("viewer").classList.remove("hidden");
        loadScreenshot();
        return;
      }
      busy(button, async () => {
        try {
          done(await SYSTEM[action]());
        } catch (error) {
          if (error.status !== 401) toast(error.message, true);
        }
      });
    });
  });

  // --- избранные команды --------------------------------------------------------------------------
  const COMMAND_ICONS = {
    timer: "clock",
    weather: "cloud",
    media: "note",
    spotify: "note",
    window: "windows",
    youtube: "video",
    volume: "speaker",
    mute: "mute",
    lock: "lock",
    power: "power",
    screenshot: "camera",
  };

  async function loadCommands() {
    const box = $("commands");
    try {
      const { items } = await api("commands");
      box.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Избранных команд пока нет — добавьте их в приложении Jarvis.";
        box.appendChild(empty);
        return;
      }
      for (const item of items) {
        const button = document.createElement("button");
        button.className = "tile command";
        button.appendChild(makeIcon(COMMAND_ICONS[item.icon] || "spark"));
        const label = document.createElement("span");
        label.textContent = item.title;
        button.appendChild(label);
        button.addEventListener("click", () => {
          buzz();
          busy(button, async () => {
            try {
              const result = await api("run/" + encodeURIComponent(item.id), { json: {} });
              const answer = result.result && result.result.kind === "ai" ? result.result : null;
              if (answer) showAnswer(answer);
              else done(result, item.title);
            } catch (error) {
              if (error.status !== 401) toast(error.message, true);
            }
          });
        });
        box.appendChild(button);
      }
    } catch (error) {
      if (error.status !== 401) {
        box.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "Команды не загрузились: " + error.message;
        box.appendChild(empty);
      }
    }
  }

  // --- «Скажите Джарвису…» ------------------------------------------------------------------------
  function showAnswer(result) {
    const box = $("answer");
    const failed = result.ok === false;
    box.classList.remove("hidden");
    box.classList.toggle("error", failed);
    $("answer-title").textContent = result.title || (failed ? "Не получилось" : "Готово");
    $("answer-text").textContent = result.text || result.message || "";
  }

  $("answer-close").addEventListener("click", () => $("answer").classList.add("hidden"));

  async function send(text) {
    const clean = text.trim();
    if (!clean) return;
    const sendButton = $("send");
    sendButton.disabled = true;
    $("answer").classList.remove("hidden", "error");
    $("answer-title").textContent = "Jarvis думает…";
    $("answer-text").textContent = clean;
    try {
      const result = await api("dispatch", { json: { text: clean } });
      if (result.kind === "empty") $("answer").classList.add("hidden");
      else showAnswer(result);
      $("ask").value = "";
      setTimeout(refresh, 500);
    } catch (error) {
      if (error.status !== 401) showAnswer({ ok: false, title: "Не получилось", message: error.message });
    } finally {
      sendButton.disabled = false;
    }
  }

  $("ask-form").addEventListener("submit", (event) => {
    event.preventDefault();
    send($("ask").value);
  });

  // голосовой ввод браузера (Web Speech API); по http в домашней сети браузеры часто запрещают микрофон —
  // тогда подскажем диктовку с клавиатуры телефона
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (Recognition) {
    const mic = $("mic");
    mic.classList.remove("hidden");
    let recognition = null;
    mic.addEventListener("click", () => {
      buzz();
      if (recognition) {
        recognition.stop();
        return;
      }
      if (!window.isSecureContext) {
        toast("Браузер не даёт микрофон по http — используйте микрофон на клавиатуре телефона", true);
        $("ask").focus();
        return;
      }
      recognition = new Recognition();
      recognition.lang = "ru-RU";
      recognition.interimResults = true;
      recognition.maxAlternatives = 1;
      let finalText = "";
      recognition.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const piece = event.results[i][0].transcript;
          if (event.results[i].isFinal) finalText += piece;
          else interim += piece;
        }
        $("ask").value = (finalText + interim).trim();
      };
      recognition.onerror = (event) => {
        const blocked = event.error === "not-allowed" || event.error === "service-not-allowed";
        if (blocked) toast("Нет доступа к микрофону — разрешите его или наберите текст", true);
        else if (event.error !== "no-speech" && event.error !== "aborted") toast("Не расслышал", true);
      };
      recognition.onend = () => {
        mic.classList.remove("listening");
        recognition = null;
        if (finalText.trim()) send(finalText);
      };
      mic.classList.add("listening");
      try {
        recognition.start();
      } catch (_) {
        mic.classList.remove("listening");
        recognition = null;
      }
    });
  }

  // --- старт --------------------------------------------------------------------------------------
  if (!token) {
    showDenied();
    return;
  }
  startPolling();
  loadCommands();
})();
