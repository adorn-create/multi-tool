const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const form = document.querySelector("#task-form");
const promptInput = document.querySelector("#prompt");
const authorizedInput = document.querySelector("#authorized");
const sendButton = document.querySelector("#send-button");
const taskMessage = document.querySelector("#task-message");
const boardMessage = document.querySelector("#board-message");
const idleTimeoutMinutes = Number(document.querySelector('meta[name="idle-timeout-minutes"]').content);
const idleTimeoutMs = idleTimeoutMinutes * 60 * 1000;
const heartbeatMs = 60 * 1000;
let lastActivityAt = Date.now();
let lastHeartbeatAt = lastActivityAt;
let logoutTimer;
let loggingOut = false;
let boardLoading = false;

const taskColumns = [
  { statuses: ["triage"], list: document.querySelector("#tasks-triage") },
  { statuses: ["todo", "ready"], list: document.querySelector("#tasks-queued") },
  { statuses: ["running"], list: document.querySelector("#tasks-running") },
  { statuses: ["blocked", "review"], list: document.querySelector("#tasks-review") },
  { statuses: ["done", "archived"], list: document.querySelector("#tasks-done") },
];

function signOutForInactivity() {
  if (loggingOut) return;
  loggingOut = true;
  const body = new URLSearchParams({ csrf_token: csrfToken });
  fetch("/logout", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
    keepalive: true,
  }).finally(() => {
    window.location.replace("/login?expired=1");
  });
}

function noteActivity() {
  if (loggingOut) return;
  lastActivityAt = Date.now();
  window.clearTimeout(logoutTimer);
  logoutTimer = window.setTimeout(signOutForInactivity, idleTimeoutMs);

  if (lastActivityAt - lastHeartbeatAt >= heartbeatMs) {
    lastHeartbeatAt = lastActivityAt;
    postJson("/api/activity", {}).catch(() => {});
  }
}

["pointerdown", "keydown", "scroll", "touchstart"].forEach((eventName) => {
  document.addEventListener(eventName, noteActivity, { passive: true });
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    if (Date.now() - lastActivityAt >= idleTimeoutMs) signOutForInactivity();
    else noteActivity();
  }
});
window.setInterval(() => {
  if (
    document.visibilityState === "visible" &&
    Date.now() - lastActivityAt < heartbeatMs &&
    Date.now() - lastHeartbeatAt >= heartbeatMs
  ) {
    lastHeartbeatAt = Date.now();
    postJson("/api/activity", {}).catch(() => {});
  }
}, heartbeatMs);
logoutTimer = window.setTimeout(signOutForInactivity, idleTimeoutMs);

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(payload),
  });
  if (response.status === 401) {
    window.location.assign("/login?expired=1");
    throw new Error("Your session ended. Sign in again.");
  }
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request could not be completed.");
  return result;
}

function showTask(task, list) {
  const card = document.createElement("article");
  card.className = "task-card";

  const heading = document.createElement("h4");
  heading.textContent = task.title;
  const status = document.createElement("span");
  status.className = `task-status status-${task.status}`;
  status.textContent = task.status.replaceAll("_", " ");
  const assignee = document.createElement("p");
  assignee.className = "task-assignee";
  assignee.textContent = task.assignee ? `Assigned to ${task.assignee}` : "Waiting for assignment";
  card.append(heading, status, assignee);

  if (typeof task.result === "string" && task.result.trim()) {
    const result = document.createElement("p");
    result.className = "task-result";
    result.textContent = task.result.trim();
    card.append(result);
  }
  list.append(card);
}

function renderBoard(tasks) {
  taskColumns.forEach(({ list }) => list.replaceChildren());
  const counts = new Map(taskColumns.map(({ list }) => [list, 0]));

  tasks.forEach((task) => {
    const column = taskColumns.find(({ statuses }) => statuses.includes(task.status));
    if (!column) throw new Error(`Hermes returned an unsupported task status: ${task.status}`);
    showTask(task, column.list);
    counts.set(column.list, counts.get(column.list) + 1);
  });

  taskColumns.forEach(({ list }) => {
    list.closest(".kanban-column").querySelector(".column-count").textContent = counts.get(list);
    if (!list.childElementCount) {
      const empty = document.createElement("p");
      empty.className = "empty-column";
      empty.textContent = "No tasks here";
      list.append(empty);
    }
  });
}

async function loadBoard() {
  if (boardLoading) return;
  boardLoading = true;
  boardMessage.textContent = "Syncing with the local Hermes board…";
  boardMessage.className = "board-message";
  try {
    const response = await fetch("/api/team", { headers: { Accept: "application/json" } });
    if (response.status === 401) {
      window.location.assign("/login?expired=1");
      throw new Error("Your session ended. Sign in again.");
    }
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The team board could not be loaded.");
    if (!Array.isArray(result.tasks)) throw new Error("Hermes returned an unexpected task list.");
    renderBoard(result.tasks);
    boardMessage.textContent = `Synced ${result.tasks.length} task${result.tasks.length === 1 ? "" : "s"}.`;
  } catch (error) {
    boardMessage.textContent = error.message;
    boardMessage.className = "board-message error-text";
  } finally {
    boardLoading = false;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const prompt = promptInput.value.trim();
  if (!prompt || !authorizedInput.checked || sendButton.disabled) {
    form.reportValidity();
    return;
  }

  sendButton.disabled = true;
  sendButton.textContent = "Adding task…";
  taskMessage.className = "form-note";
  taskMessage.textContent = "Sending the task to Hermes Kanban…";
  try {
    const result = await postJson("/api/team/tasks", { prompt, authorized: true });
    promptInput.value = "";
    promptInput.style.height = "auto";
    authorizedInput.checked = false;
    taskMessage.textContent = `Task ${result.task.id} added to triage.`;
    await loadBoard();
  } catch (error) {
    taskMessage.textContent = error.message;
    taskMessage.className = "form-note error-text";
  } finally {
    sendButton.disabled = false;
    sendButton.innerHTML = 'Add to team queue <span aria-hidden="true">↗</span>';
    promptInput.focus();
  }
});

promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 180)}px`;
});

document.querySelector("#refresh-board").addEventListener("click", loadBoard);
window.setInterval(() => {
  if (document.visibilityState === "visible") loadBoard();
}, 15000);
loadBoard();
