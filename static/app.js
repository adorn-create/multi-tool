const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const form = document.querySelector("#chat-form");
const promptInput = document.querySelector("#prompt");
const sendButton = document.querySelector("#send-button");
const conversation = document.querySelector("#conversation");
const welcome = document.querySelector("#welcome");
const idleTimeoutMinutes = Number(document.querySelector('meta[name="idle-timeout-minutes"]').content);
const idleTimeoutMs = idleTimeoutMinutes * 60 * 1000;
const heartbeatMs = 60 * 1000;
let lastActivityAt = Date.now();
let lastHeartbeatAt = lastActivityAt;
let logoutTimer;
let loggingOut = false;

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

function addMessage(speaker, text, isUser = false) {
  const article = document.createElement("article");
  article.className = `message ${isUser ? "user" : "assistant"}`;

  const label = document.createElement("p");
  label.className = "message-label";
  label.textContent = speaker;
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  article.append(label, body);
  conversation.append(article);
  article.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return { article, body };
}

function showError(container, message) {
  const error = document.createElement("p");
  error.className = "result-output error-text";
  error.textContent = message;
  container.append(error);
}

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

async function askAssistant(prompt) {
  welcome.hidden = true;
  const userMessage = addMessage("YOU", prompt, true);
  const assistant = addMessage("NIGASH", "Thinking…");
  sendButton.disabled = true;
  try {
    const result = await postJson("/api/chat", { prompt });
    assistant.body.textContent = result.message;
    if (result.action === "command") {
      const card = document.createElement("section");
      card.className = "command-card";
      const title = document.createElement("p");
      title.className = "command-title";
      title.textContent = "Review before running";
      const command = document.createElement("pre");
      command.className = "command-preview";
      command.textContent = `$ ${result.command}`;
      const button = document.createElement("button");
      button.className = "run-button";
      button.type = "button";
      button.textContent = "Run on my VM";
      card.append(title, command, button);
      assistant.body.append(card);

      button.addEventListener("click", async () => {
        if (!window.confirm(`Run this command on your configured Kali VM?\n\n${result.command}`)) return;
        button.disabled = true;
        button.textContent = "Running…";
        try {
          const execution = await postJson("/api/execute", {
            command: result.command,
            confirmation: result.confirmation,
          });
          const output = document.createElement("pre");
          output.className = "result-output";
          output.textContent = execution.result;
          card.append(output);
          button.remove();
        } catch (error) {
          button.disabled = false;
          button.textContent = "Retry command";
          showError(card, error.message);
        }
      });
    }
  } catch (error) {
    assistant.body.textContent = "";
    showError(assistant.body, error.message);
  } finally {
    sendButton.disabled = false;
    promptInput.focus();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const prompt = promptInput.value.trim();
  if (!prompt || sendButton.disabled) return;
  promptInput.value = "";
  promptInput.style.height = "auto";
  askAssistant(prompt);
});

promptInput.addEventListener("input", () => {
  promptInput.style.height = "auto";
  promptInput.style.height = `${Math.min(promptInput.scrollHeight, 160)}px`;
});

promptInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    form.requestSubmit();
  }
});

document.querySelectorAll(".suggestion").forEach((button) => {
  button.addEventListener("click", () => {
    promptInput.value = button.dataset.prompt;
    form.requestSubmit();
  });
});

document.querySelectorAll(".install-tool-button").forEach((button) => {
  button.addEventListener("click", async () => {
    const packageName = button.dataset.package;
    if (!window.confirm(
      `Install ${packageName} from the Kali APT repositories on your configured VM? ` +
      "This runs apt-get update and requires passwordless sudo."
    )) return;

    button.disabled = true;
    button.textContent = "Installing…";
    const card = button.closest(".tool-card");
    try {
      const result = await postJson("/api/tools/install", {
        package: packageName,
        confirmation: button.dataset.confirmation,
      });
      const output = document.createElement("pre");
      output.className = "result-output";
      output.textContent = result.result;
      card.append(output);
      button.textContent = "Finished";
    } catch (error) {
      button.disabled = false;
      button.textContent = "Retry install";
      showError(card, error.message);
    }
  });
});
