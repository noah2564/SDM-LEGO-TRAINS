/* Depot - LEGO train control UI.
 *
 * All state changes arrive over the WebSocket; HTTP is only used to send
 * commands and edit configuration. The browser never touches MQTT.
 */
(() => {
  "use strict";

  const API = "/api";
  const state = {
    trains: new Map(),
    stats: null,
    discovered: [],
    openTrainId: null,
    events: [],
    socket: null,
    linkUp: false,
    retry: 0,
    pendingSpeed: new Map(),  // train id -> timer, so dragging does not flood
  };

  const $ = (selector) => document.querySelector(selector);
  const fleetEl = $("#fleet");

  // ---------------------------------------------------------------- http
  async function request(path, options = {}) {
    const response = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (response.status === 204) return null;
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(body.detail ? formatDetail(body.detail) : `Request failed (${response.status})`);
    }
    return body;
  }

  function formatDetail(detail) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => `${(item.loc || []).slice(1).join(".")}: ${item.msg}`).join("; ");
    }
    return JSON.stringify(detail);
  }

  function toast(message, kind = "info") {
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.kind = kind;
    node.textContent = message;
    $("#toasts").appendChild(node);
    setTimeout(() => node.remove(), kind === "error" ? 6000 : 3200);
  }

  async function send(trainId, command) {
    try {
      await request(`/trains/${encodeURIComponent(trainId)}/command`, {
        method: "POST",
        body: JSON.stringify(command),
      });
    } catch (error) {
      toast(error.message, "error");
      loadTrains();   // resync: our optimistic state may now be wrong
    }
  }

  // ------------------------------------------------------------ rendering
  function statusLabel(train) {
    if (train.emergency_stop) return "Emergency stop";
    return { online: "Online", offline: "Offline", connecting: "Connecting", unknown: "No signal" }[train.status]
      || "Unknown";
  }

  function lampState(train) {
    if (train.emergency_stop) return "error";
    return train.status;
  }

  function relativeTime(iso) {
    if (!iso) return "never";
    const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (seconds < 5) return "just now";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
    return new Date(iso).toLocaleDateString();
  }

  function render() {
    const trains = [...state.trains.values()].sort((a, b) => a.id.localeCompare(b.id));
    $("#empty").hidden = trains.length > 0;
    fleetEl.hidden = trains.length === 0;

    const seen = new Set();
    for (const train of trains) {
      seen.add(train.id);
      let plate = fleetEl.querySelector(`[data-train="${CSS.escape(train.id)}"]`);
      if (!plate) {
        plate = buildPlate(train);
        fleetEl.appendChild(plate);
      }
      updatePlate(plate, train);
    }
    for (const plate of [...fleetEl.children]) {
      if (!seen.has(plate.dataset.train)) plate.remove();
    }

    renderStats(trains);
    renderDiscovery();
    if (state.openTrainId) renderDrawer();
  }

  function renderStats(trains) {
    const counts = { total: trains.length, online: 0, offline: 0, moving: 0 };
    let latched = 0;
    for (const train of trains) {
      if (train.status === "online") counts.online += 1;
      if (train.status === "offline" || train.status === "unknown") counts.offline += 1;
      if (train.status === "online" && train.speed > 0) counts.moving += 1;
      if (train.emergency_stop) latched += 1;
    }
    $("#gauge-total").querySelector("b").textContent = counts.total;
    $("#gauge-online").querySelector("b").textContent = counts.online;
    $("#gauge-offline").querySelector("b").textContent = counts.offline;
    $("#gauge-moving").querySelector("b").textContent = counts.moving;
    $("#estop-clear").hidden = latched === 0;
  }

  function renderDiscovery() {
    const section = $("#discovery");
    const list = $("#discovery-list");
    const unknown = state.discovered.filter((item) => !state.trains.has(item.train_id));
    section.hidden = unknown.length === 0;
    list.innerHTML = "";
    for (const item of unknown) {
      const row = document.createElement("div");
      row.className = "discovery-item";
      row.innerHTML = `<span class="lamp" data-state="online"></span>
        <span><b>${escapeHtml(item.train_id)}</b>${item.device_id ? ` &middot; ${escapeHtml(item.device_id)}` : ""}</span>`;
      const button = document.createElement("button");
      button.className = "ghost";
      button.textContent = "Register";
      button.onclick = () => openDialog(null, { id: item.train_id, device_id: item.device_id || "" });
      row.appendChild(button);
      list.appendChild(row);
    }
  }

  function buildPlate(train) {
    const plate = document.createElement("article");
    plate.className = "plate";
    plate.dataset.train = train.id;
    plate.innerHTML = `
      <div class="plate-head">
        <div>
          <div class="plate-id"><span class="lamp"></span><h2></h2></div>
          <p class="plate-meta"></p>
        </div>
        <div class="plate-state"><b></b><span class="plate-seen"></span></div>
      </div>
      <div class="readout">
        <span class="readout-speed">0</span><span class="readout-unit">speed</span>
        <span class="readout-dir"></span>
      </div>
      <div class="throttle">
        <input type="range" min="0" max="100" step="5" value="0" aria-label="Speed">
        <div class="ticks"><span>0</span><span>25</span><span>50</span><span>75</span><span>100</span></div>
      </div>
      <div class="controls">
        <button data-act="backward">Reverse</button>
        <button data-act="stop" class="halt">Stop</button>
        <button data-act="forward">Forward</button>
      </div>
      <div class="plate-foot">
        <span class="foot-note"></span>
        <button data-act="details">Details</button>
      </div>`;

    const slider = plate.querySelector("input[type=range]");
    slider.addEventListener("input", () => {
      plate.querySelector(".readout-speed").textContent = slider.value;
      queueSpeed(train.id, Number(slider.value));
    });
    plate.querySelector('[data-act="forward"]').onclick = () => drive(train.id, "forward");
    plate.querySelector('[data-act="backward"]').onclick = () => drive(train.id, "backward");
    plate.querySelector('[data-act="stop"]').onclick = () => {
      slider.value = 0;
      request(`/trains/${encodeURIComponent(train.id)}/stop`, { method: "POST" })
        .catch((error) => toast(error.message, "error"));
    };
    plate.querySelector('[data-act="details"]').onclick = () => openDrawer(train.id);
    return plate;
  }

  function updatePlate(plate, train) {
    const offline = train.status === "offline" || train.status === "unknown";
    plate.dataset.status = train.status;
    plate.dataset.estop = String(train.emergency_stop);
    plate.classList.toggle("is-offline", offline);

    plate.querySelector(".lamp").dataset.state = lampState(train);
    plate.querySelector("h2").textContent = train.name;
    plate.querySelector(".plate-meta").textContent = `${train.id} · ${train.device_id}`;
    plate.querySelector(".plate-state b").textContent = statusLabel(train);
    plate.querySelector(".plate-seen").textContent = `seen ${relativeTime(train.last_seen)}`;

    const slider = plate.querySelector("input[type=range]");
    if (document.activeElement !== slider && !state.pendingSpeed.has(train.id)) {
      slider.value = train.speed;
      plate.querySelector(".readout-speed").textContent = train.speed;
    }
    slider.max = train.max_speed;
    slider.disabled = offline || train.emergency_stop;

    plate.querySelector(".readout-dir").innerHTML =
      train.direction === "backward" ? `<span>&#9664;</span> Reverse` : `Forward <span>&#9654;</span>`;

    for (const button of plate.querySelectorAll(".controls button")) {
      const action = button.dataset.act;
      button.disabled = offline || (train.emergency_stop && action !== "stop");
      button.classList.toggle(
        "active",
        !offline && train.speed > 0 && action === train.direction
      );
    }

    const foot = plate.querySelector(".foot-note");
    if (train.emergency_stop) {
      foot.className = "foot-note latched";
      foot.innerHTML = "Emergency stop latched";
      const release = document.createElement("button");
      release.textContent = "Clear";
      release.onclick = () =>
        request(`/trains/${encodeURIComponent(train.id)}/clear-emergency`, { method: "POST" })
          .catch((error) => toast(error.message, "error"));
      foot.appendChild(release);
    } else if (train.last_error) {
      foot.className = "foot-note latched";
      foot.textContent = train.last_error;
    } else {
      foot.className = "foot-note";
      foot.textContent = train.battery != null ? `Battery ${train.battery.toFixed(1)} V` : "";
    }
  }

  function drive(trainId, direction) {
    const train = state.trains.get(trainId);
    if (!train) return;
    const plate = fleetEl.querySelector(`[data-train="${CSS.escape(trainId)}"]`);
    const slider = plate.querySelector("input[type=range]");
    // Pressing a direction with the throttle closed starts a gentle crawl.
    const speed = Number(slider.value) > 0 ? Number(slider.value) : Math.min(25, train.max_speed);
    slider.value = speed;
    send(trainId, { command: "set_speed", speed, direction });
  }

  function queueSpeed(trainId, speed) {
    const existing = state.pendingSpeed.get(trainId);
    if (existing) clearTimeout(existing);
    state.pendingSpeed.set(
      trainId,
      setTimeout(() => {
        state.pendingSpeed.delete(trainId);
        send(trainId, { command: "set_speed", speed });
      }, 120)
    );
  }

  // -------------------------------------------------------------- drawer
  async function openDrawer(trainId) {
    state.openTrainId = trainId;
    $("#drawer").hidden = false;
    $("#scrim").hidden = false;
    await loadEvents(trainId);
    renderDrawer();
  }

  function closeDrawer() {
    state.openTrainId = null;
    $("#drawer").hidden = true;
    $("#scrim").hidden = true;
  }

  async function loadEvents(trainId) {
    try {
      state.events = await request(`/trains/${encodeURIComponent(trainId)}/events?limit=40`);
    } catch (error) {
      state.events = [];
    }
  }

  function renderDrawer() {
    const train = state.trains.get(state.openTrainId);
    if (!train) return closeDrawer();

    $("#drawer-name").textContent = train.name;
    $("#drawer-sub").textContent = `${train.id} · ${train.device_id} · ${statusLabel(train)}`;

    const telemetry = train.last_telemetry || {};
    const rows = [
      ["Connection", statusLabel(train)],
      ["Speed", `${train.speed} of ${train.max_speed}`],
      ["Direction", train.direction === "backward" ? "Reverse" : "Forward"],
      ["Battery", train.battery != null ? `${train.battery.toFixed(2)} V` : "not reported"],
      ["Signal", telemetry.rssi != null ? `${telemetry.rssi} dBm` : "not reported"],
      ["Device uptime", telemetry.uptime_s != null ? `${Math.round(telemetry.uptime_s)} s` : "not reported"],
      ["Last message", relativeTime(train.last_message_at)],
      ["Last seen", relativeTime(train.last_seen)],
      ["Last command", relativeTime(train.last_command_at)],
      ["Registered", new Date(train.created_at).toLocaleString()],
      ["Description", train.description || "—"],
      ["Last error", train.last_error || "none"],
    ];

    const extra = telemetry.extra
      ? Object.entries(telemetry.extra).map(([key, value]) => [key, String(value)])
      : [];

    $("#drawer-body").innerHTML = `
      <section>
        <h3>State</h3>
        <dl class="kv">${[...rows, ...extra]
          .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(String(value))}</dd>`)
          .join("")}</dl>
      </section>
      <section>
        <h3>Recent activity</h3>
        <div class="log">${state.events.length
          ? state.events.map(eventRow).join("")
          : '<p class="plate-meta">Nothing logged yet.</p>'}</div>
      </section>
      <section>
        <h3>Configuration</h3>
        <menu style="justify-content:flex-start">
          <button class="ghost" id="drawer-edit">Edit train</button>
          <button class="danger" id="drawer-delete">Remove train</button>
        </menu>
      </section>`;

    $("#drawer-edit").onclick = () => openDialog(train);
    $("#drawer-delete").onclick = async () => {
      if (!confirm(`Remove ${train.name}? Its history is deleted too.`)) return;
      try {
        await request(`/trains/${encodeURIComponent(train.id)}`, { method: "DELETE" });
        closeDrawer();
        toast(`${train.name} removed`, "ok");
      } catch (error) {
        toast(error.message, "error");
      }
    };
  }

  function eventRow(event) {
    const time = new Date(event.created_at).toLocaleTimeString();
    return `<div class="log-row" data-severity="${escapeHtml(event.severity)}">
        <time>${escapeHtml(time)}</time>
        <span><span class="log-type">${escapeHtml(event.type)}</span> ${escapeHtml(event.message || "")}</span>
      </div>`;
  }

  // -------------------------------------------------------------- dialog
  const dialog = $("#train-dialog");
  let editing = null;

  function openDialog(train, prefill) {
    editing = train;
    const form = $("#train-form");
    form.reset();
    $("#form-error").hidden = true;
    $("#dialog-title").textContent = train ? `Edit ${train.name}` : "Add a train";
    $("#dialog-submit").textContent = train ? "Save changes" : "Add train";
    const source = train || prefill || {};
    form.id.value = source.id || "";
    form.id.disabled = Boolean(train);
    form.name.value = source.name || "";
    form.device_id.value = source.device_id || "";
    form.description.value = source.description || "";
    form.max_speed.value = source.max_speed || 100;
    dialog.showModal();
  }

  $("#train-form").addEventListener("submit", async (submitEvent) => {
    submitEvent.preventDefault();
    const form = submitEvent.target;
    const payload = {
      name: form.name.value.trim(),
      device_id: form.device_id.value.trim(),
      description: form.description.value.trim() || null,
      max_speed: Number(form.max_speed.value),
    };
    try {
      if (editing) {
        await request(`/trains/${encodeURIComponent(editing.id)}`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        toast("Changes saved", "ok");
      } else {
        await request("/trains", {
          method: "POST",
          body: JSON.stringify({ id: form.id.value.trim(), ...payload }),
        });
        toast("Train added", "ok");
        loadDiscovered();
      }
      dialog.close();
    } catch (error) {
      const errorNode = $("#form-error");
      errorNode.textContent = error.message;
      errorNode.hidden = false;
    }
  });

  $("#dialog-cancel").onclick = () => dialog.close();
  $("#add-train").onclick = () => openDialog(null);
  $("#empty-add").onclick = () => openDialog(null);
  $("#drawer-close").onclick = closeDrawer;
  $("#scrim").onclick = closeDrawer;
  document.addEventListener("keydown", (keyEvent) => {
    if (keyEvent.key === "Escape" && state.openTrainId) closeDrawer();
  });

  // ------------------------------------------------------- emergency stop
  $("#estop").onclick = async () => {
    try {
      const result = await request("/emergency-stop", {
        method: "POST",
        body: JSON.stringify({ reason: "operator pressed emergency stop" }),
      });
      toast(`Emergency stop sent to ${result.trains.length} train(s)`, "error");
    } catch (error) {
      toast(error.message, "error");
    }
  };

  $("#estop-clear").onclick = async () => {
    const latched = [...state.trains.values()].filter((train) => train.emergency_stop);
    for (const train of latched) {
      try {
        await request(`/trains/${encodeURIComponent(train.id)}/clear-emergency`, { method: "POST" });
      } catch (error) {
        toast(`${train.name}: ${error.message}`, "error");
      }
    }
  };

  // ---------------------------------------------------------- data + live
  async function loadTrains() {
    try {
      const trains = await request("/trains");
      state.trains = new Map(trains.map((train) => [train.id, train]));
      render();
    } catch (error) {
      toast(`Cannot reach the control server: ${error.message}`, "error");
    }
  }

  async function loadDiscovered() {
    try {
      state.discovered = await request("/discovered");
      renderDiscovery();
    } catch (error) {
      /* discovery is a convenience; a failure here is not worth a toast */
    }
  }

  function setLink(up, text) {
    state.linkUp = up;
    $("#link-lamp").dataset.state = up ? "online" : "offline";
    $("#link-text").textContent = text;
  }

  function connect() {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${scheme}://${location.host}/ws`);
    state.socket = socket;

    socket.onopen = () => {
      state.retry = 0;
      setLink(true, "Live");
      loadDiscovered();
    };

    socket.onmessage = (messageEvent) => {
      const message = JSON.parse(messageEvent.data);
      const data = message.data;
      switch (message.type) {
        case "snapshot":
          state.trains = new Map(data.trains.map((train) => [train.id, train]));
          state.stats = data.stats;
          setLink(
            true,
            data.stats.mqtt_connected ? "Live · broker connected" : "Live · broker unreachable"
          );
          render();
          break;
        case "train.updated":
        case "train.created":
          state.trains.set(data.id, data);
          render();
          break;
        case "train.deleted":
          state.trains.delete(data.id);
          if (state.openTrainId === data.id) closeDrawer();
          render();
          break;
        case "event":
          if (state.openTrainId === data.train_id) {
            state.events.unshift(data);
            state.events = state.events.slice(0, 40);
            renderDrawer();
          }
          break;
        case "fleet.emergency_stop":
          loadTrains();
          break;
        case "discovery":
          loadDiscovered();
          break;
        default:
          break;
      }
    };

    socket.onclose = () => {
      setLink(false, "Reconnecting to the control server");
      state.retry = Math.min(state.retry + 1, 6);
      setTimeout(connect, 500 * 2 ** (state.retry - 1));
    };

    socket.onerror = () => socket.close();
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character])
    );
  }

  // Keep relative timestamps honest without polling the API.
  setInterval(() => { if (state.trains.size) render(); }, 5000);
  // Keepalive so proxies do not drop an idle WebSocket.
  setInterval(() => {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) state.socket.send("ping");
  }, 25000);

  loadTrains().then(connect);
})();
