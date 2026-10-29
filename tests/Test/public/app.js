const QUICK_PROMPTS = [
  {
    title: "Analyze Workspace",
    body: "Summarize the current project structure.",
    prompt: "/analyze",
  },
  {
    title: "Search Code",
    body: "Find a symbol, string, or implementation detail.",
    prompt: "/search TODO",
  },
  {
    title: "Read Active File",
    body: "Inspect the file that is open in the editor.",
    prompt: "/read README.md",
  },
  {
    title: "Run Tests",
    body: "Execute a command and inspect the result.",
    prompt: "/run npm test",
  },
];

const STORAGE_KEY = "moonwalk.ide.agent.config";

const state = {
  config: null,
  tree: new Map(),
  expandedDirectories: new Set([""]),
  sidebarMode: "explorer",
  searchQuery: "",
  searchResults: [],
  searchLoading: false,
  searchToken: 0,
  explorerSelection: "",
  openTabs: [],
  documents: new Map(),
  activePath: "",
  terminal: {
    sessionId: null,
    events: [],
    status: "idle",
    source: null,
  },
  agent: {
    runId: null,
    status: "idle",
    events: [],
    source: null,
    providerMode: "local-planner",
    cwd: "",
    prompt: "/analyze",
    config: loadAgentConfig(),
  },
  notice: "Booting workspace…",
};

const dom = {
  workspaceRoot: document.querySelector("#workspaceRoot"),
  providerChip: document.querySelector("#providerChip"),
  terminalChip: document.querySelector("#terminalChip"),
  dirtyChip: document.querySelector("#dirtyChip"),
  globalStatus: document.querySelector("#globalStatus"),
  explorerModeButton: document.querySelector("#explorerModeButton"),
  searchModeButton: document.querySelector("#searchModeButton"),
  explorerPane: document.querySelector("#explorerPane"),
  searchPane: document.querySelector("#searchPane"),
  explorerEmpty: document.querySelector("#explorerEmpty"),
  treeRoot: document.querySelector("#treeRoot"),
  searchInput: document.querySelector("#searchInput"),
  runSearchButton: document.querySelector("#runSearchButton"),
  searchResults: document.querySelector("#searchResults"),
  refreshTreeButton: document.querySelector("#refreshTreeButton"),
  newFileButton: document.querySelector("#newFileButton"),
  newFolderButton: document.querySelector("#newFolderButton"),
  renameButton: document.querySelector("#renameButton"),
  deleteButton: document.querySelector("#deleteButton"),
  saveButton: document.querySelector("#saveButton"),
  revertButton: document.querySelector("#revertButton"),
  tabStrip: document.querySelector("#tabStrip"),
  editorPlaceholder: document.querySelector("#editorPlaceholder"),
  editorSurface: document.querySelector("#editorSurface"),
  editorMeta: document.querySelector("#editorMeta"),
  editorGutter: document.querySelector("#editorGutter"),
  editorTextarea: document.querySelector("#editorTextarea"),
  terminalOutput: document.querySelector("#terminalOutput"),
  terminalInput: document.querySelector("#terminalInput"),
  sendTerminalButton: document.querySelector("#sendTerminalButton"),
  clearTerminalButton: document.querySelector("#clearTerminalButton"),
  restartTerminalButton: document.querySelector("#restartTerminalButton"),
  agentPrompt: document.querySelector("#agentPrompt"),
  agentCwdInput: document.querySelector("#agentCwdInput"),
  agentBaseUrlInput: document.querySelector("#agentBaseUrlInput"),
  agentModelInput: document.querySelector("#agentModelInput"),
  agentApiKeyInput: document.querySelector("#agentApiKeyInput"),
  runAgentButton: document.querySelector("#runAgentButton"),
  clearAgentButton: document.querySelector("#clearAgentButton"),
  quickPrompts: document.querySelector("#quickPrompts"),
  agentStream: document.querySelector("#agentStream"),
};

function loadAgentConfig() {
  try {
    return JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "{}");
  } catch {
    return {};
  }
}

function saveAgentConfig() {
  window.localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      baseUrl: state.agent.config.baseUrl || "",
      model: state.agent.config.model || "",
    }),
  );
}

function escapeHtml(value) {
  return `${value ?? ""}`
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("'", "&#39;");
}

function stripAnsi(value) {
  return `${value ?? ""}`.replaceAll(
    /\u001b\[[0-9;]*m/gu,
    "",
  );
}

function formatTimestamp(value) {
  if (!value) {
    return "";
  }

  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatBytes(value) {
  if (value == null) {
    return "";
  }

  if (value < 1024) {
    return `${value} B`;
  }

  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }

  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function dirname(relativePath = "") {
  if (!relativePath || !relativePath.includes("/")) {
    return "";
  }

  return relativePath.split("/").slice(0, -1).join("/");
}

function basename(relativePath = "") {
  return relativePath.split("/").filter(Boolean).pop() || relativePath || ".";
}

function getKnownItemKind(relativePath) {
  for (const node of state.tree.values()) {
    const match = node?.items?.find((item) => item.path === relativePath);

    if (match) {
      return match.kind;
    }
  }

  if (state.documents.has(relativePath)) {
    return "file";
  }

  return "";
}

function getDirtyCount() {
  return Array.from(state.documents.values()).filter((doc) => doc.dirty).length;
}

function setNotice(message) {
  state.notice = message;
  renderHeader();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
    },
    ...options,
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}.`;

    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch {
      message = await response.text();
    }

    throw new Error(message);
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

function renderHeader() {
  dom.workspaceRoot.textContent = state.config?.workspaceRoot || "Loading…";
  dom.providerChip.textContent = `agent: ${state.agent.providerMode}`;
  dom.terminalChip.textContent = `terminal: ${state.terminal.status}`;
  dom.dirtyChip.textContent = getDirtyCount()
    ? `${getDirtyCount()} unsaved`
    : "clean";
  dom.globalStatus.textContent = state.notice;
}

function setSidebarMode(mode) {
  state.sidebarMode = mode;
  dom.explorerModeButton.classList.toggle("is-active", mode === "explorer");
  dom.searchModeButton.classList.toggle("is-active", mode === "search");
  dom.explorerPane.classList.toggle("hidden", mode !== "explorer");
  dom.searchPane.classList.toggle("hidden", mode !== "search");
}

async function loadDirectory(relativePath = "") {
  const pathKey = relativePath || "";
  state.tree.set(pathKey, {
    ...(state.tree.get(pathKey) || {}),
    loading: true,
  });
  renderExplorer();

  const result = await api(`/api/tree?path=${encodeURIComponent(pathKey)}`);
  state.tree.set(pathKey, {
    ...result,
    loading: false,
  });
  renderExplorer();
}

async function refreshExpandedDirectories() {
  const directories = Array.from(state.expandedDirectories);

  for (const relativePath of directories) {
    await loadDirectory(relativePath);
  }
}

function buildTreeMarkup(relativePath = "") {
  const node = state.tree.get(relativePath || "");

  if (!node) {
    return "";
  }

  return node.items
    .map((item) => {
      const isDirectory = item.kind === "directory";
      const isExpanded = state.expandedDirectories.has(item.path);
      const isSelected = state.explorerSelection === item.path;
      const childNode = state.tree.get(item.path);
      const caret = isDirectory ? (isExpanded ? "▾" : "▸") : "·";
      const meta = isDirectory ? "dir" : formatBytes(item.size);
      const childrenMarkup =
        isDirectory && isExpanded && childNode
          ? `<div class="tree-children">${buildTreeMarkup(item.path)}</div>`
          : "";

      return `
        <div class="tree-branch">
          <button
            class="tree-row ${isDirectory ? "is-directory" : "is-file"} ${isSelected ? "is-selected" : ""}"
            data-tree-path="${escapeAttribute(item.path)}"
            data-tree-kind="${item.kind}"
            type="button"
          >
            <span class="tree-caret">${caret}</span>
            <span class="tree-name">${escapeHtml(item.name)}</span>
            <span class="tree-meta">${meta}</span>
          </button>
          ${childrenMarkup}
        </div>
      `;
    })
    .join("");
}

function renderExplorer() {
  const rootNode = state.tree.get("");
  const hasFiles = Boolean(rootNode?.items?.length);
  dom.explorerEmpty.classList.toggle("hidden", hasFiles);
  dom.treeRoot.innerHTML = hasFiles ? buildTreeMarkup("") : "";
}

async function ensureParentDirectories(relativePath) {
  const parts = relativePath.split("/").filter(Boolean);
  let current = "";

  for (let index = 0; index < parts.length - 1; index += 1) {
    current = current ? `${current}/${parts[index]}` : parts[index];
    state.expandedDirectories.add(current);

    if (!state.tree.has(current)) {
      await loadDirectory(current);
    }
  }
}

async function toggleDirectory(relativePath) {
  if (state.expandedDirectories.has(relativePath)) {
    state.expandedDirectories.delete(relativePath);
    renderExplorer();
    return;
  }

  state.expandedDirectories.add(relativePath);

  if (!state.tree.has(relativePath)) {
    await loadDirectory(relativePath);
  } else {
    renderExplorer();
  }
}

async function runSearch() {
  const query = dom.searchInput.value.trim();
  state.searchQuery = query;

  if (!query) {
    state.searchResults = [];
    renderSearchResults();
    return;
  }

  const currentToken = ++state.searchToken;
  state.searchLoading = true;
  renderSearchResults();

  try {
    const results = await api(`/api/search?q=${encodeURIComponent(query)}`);

    if (currentToken !== state.searchToken) {
      return;
    }

    state.searchResults = results;
    state.searchLoading = false;
    renderSearchResults();
    setNotice(`Search finished with ${results.length} result${results.length === 1 ? "" : "s"}.`);
  } catch (error) {
    if (currentToken !== state.searchToken) {
      return;
    }

    state.searchLoading = false;
    state.searchResults = [];
    renderSearchResults(error.message);
  }
}

function renderSearchResults(errorMessage = "") {
  if (errorMessage) {
    dom.searchResults.innerHTML = `<div class="search-result muted">${escapeHtml(errorMessage)}</div>`;
    return;
  }

  if (state.searchLoading) {
    dom.searchResults.innerHTML = `<div class="search-result muted">Searching the workspace…</div>`;
    return;
  }

  if (!state.searchQuery) {
    dom.searchResults.innerHTML = `<div class="search-result muted">Enter text to search the workspace.</div>`;
    return;
  }

  if (!state.searchResults.length) {
    dom.searchResults.innerHTML = `<div class="search-result muted">No matches for "${escapeHtml(state.searchQuery)}".</div>`;
    return;
  }

  dom.searchResults.innerHTML = state.searchResults
    .map(
      (result) => `
        <button
          class="search-result"
          data-search-path="${escapeAttribute(result.path)}"
          data-search-line="${result.line}"
          type="button"
        >
          <div class="search-path">${escapeHtml(result.path)}</div>
          <div class="search-line">Line ${result.line}</div>
          <div class="search-snippet">${escapeHtml(result.snippet)}</div>
        </button>
      `,
    )
    .join("");
}

async function openFile(relativePath, options = {}) {
  const existing = state.documents.get(relativePath);

  state.explorerSelection = relativePath;
  await ensureParentDirectories(relativePath);
  renderExplorer();

  if (!state.openTabs.includes(relativePath)) {
    state.openTabs.push(relativePath);
  }

  state.activePath = relativePath;
  renderTabs();
  renderEditor();

  if (existing && !options.forceReload) {
    if (options.line) {
      existing.pendingLine = options.line;
      moveCursorToPendingLine(existing);
    }

    renderEditor();
    return;
  }

  state.documents.set(relativePath, {
    ...(existing || {}),
    path: relativePath,
    loading: true,
    error: "",
    pendingLine: options.line || existing?.pendingLine || null,
  });
  renderEditor();

  try {
    const result = await api(`/api/file?path=${encodeURIComponent(relativePath)}`);
    state.documents.set(relativePath, {
      path: relativePath,
      content: result.content,
      savedContent: result.content,
      size: result.size,
      updatedAt: result.updatedAt,
      dirty: false,
      loading: false,
      error: "",
      pendingLine: options.line || existing?.pendingLine || null,
      externallyModified: false,
    });
    renderEditor();
    moveCursorToPendingLine(state.documents.get(relativePath));
    setNotice(`Opened ${relativePath}.`);
  } catch (error) {
    state.documents.set(relativePath, {
      path: relativePath,
      content: "",
      savedContent: "",
      dirty: false,
      loading: false,
      error: error.message,
    });
    renderEditor();
  }
}

function renderTabs() {
  if (!state.openTabs.length) {
    dom.tabStrip.innerHTML = "";
    return;
  }

  dom.tabStrip.innerHTML = state.openTabs
    .map((path) => {
      const documentState = state.documents.get(path);
      const isDirty = documentState?.dirty;
      const isActive = path === state.activePath;

      return `
        <button class="tab ${isActive ? "is-active" : ""}" data-tab-path="${escapeAttribute(path)}" type="button">
          <span class="tab-icon">${isDirty ? "●" : "○"}</span>
          <span class="tab-label">${escapeHtml(basename(path))}</span>
          <span class="tab-label muted">${escapeHtml(dirname(path))}</span>
          <span class="tab-close" data-close-path="${escapeAttribute(path)}">×</span>
        </button>
      `;
    })
    .join("");
}

function getActiveDocument() {
  return state.documents.get(state.activePath) || null;
}

function buildLineNumberMarkup(content = "") {
  const lines = content.split("\n").length || 1;
  return Array.from({ length: lines }, (_, index) => index + 1).join("\n");
}

function renderEditor() {
  const documentState = getActiveDocument();
  renderHeader();
  renderTabs();

  if (!documentState) {
    dom.editorPlaceholder.classList.remove("hidden");
    dom.editorSurface.classList.add("hidden");
    return;
  }

  dom.editorPlaceholder.classList.add("hidden");
  dom.editorSurface.classList.remove("hidden");

  if (documentState.loading) {
    dom.editorMeta.innerHTML = `<span class="muted">Loading ${escapeHtml(documentState.path)}…</span>`;
    dom.editorTextarea.value = "";
    dom.editorGutter.textContent = "1";
    return;
  }

  if (documentState.error) {
    dom.editorMeta.innerHTML = `<span class="muted">${escapeHtml(documentState.error)}</span>`;
    dom.editorTextarea.value = "";
    dom.editorGutter.textContent = "1";
    return;
  }

  dom.editorMeta.innerHTML = [
    `<span>${escapeHtml(documentState.path)}</span>`,
    `<span>${formatBytes(documentState.size)}</span>`,
    `<span>updated ${escapeHtml(formatTimestamp(documentState.updatedAt))}</span>`,
    documentState.externallyModified
      ? `<span class="muted">changed by agent or terminal</span>`
      : "",
  ]
    .filter(Boolean)
    .join("");

  if (dom.editorTextarea.value !== documentState.content) {
    dom.editorTextarea.value = documentState.content;
  }

  dom.editorGutter.textContent = buildLineNumberMarkup(documentState.content);
}

function moveCursorToPendingLine(documentState) {
  if (!documentState?.pendingLine || state.activePath !== documentState.path) {
    return;
  }

  const lineNumber = Math.max(1, Number(documentState.pendingLine) || 1);
  const lines = documentState.content.split("\n");
  let offset = 0;

  for (let index = 0; index < lineNumber - 1 && index < lines.length; index += 1) {
    offset += lines[index].length + 1;
  }

  window.requestAnimationFrame(() => {
    dom.editorTextarea.focus();
    dom.editorTextarea.setSelectionRange(offset, offset);
    const lineHeight = parseFloat(getComputedStyle(dom.editorTextarea).lineHeight || "20");
    dom.editorTextarea.scrollTop = Math.max(0, (lineNumber - 3) * lineHeight);
    dom.editorGutter.scrollTop = dom.editorTextarea.scrollTop;
  });

  documentState.pendingLine = null;
}

function closeTab(relativePath) {
  const documentState = state.documents.get(relativePath);

  if (documentState?.dirty && !window.confirm(`${relativePath} has unsaved changes. Close it anyway?`)) {
    return;
  }

  state.openTabs = state.openTabs.filter((path) => path !== relativePath);

  if (state.activePath === relativePath) {
    state.activePath = state.openTabs[state.openTabs.length - 1] || "";
  }

  renderTabs();
  renderEditor();
}

async function saveActiveDocument() {
  const documentState = getActiveDocument();

  if (!documentState || !documentState.dirty) {
    return;
  }

  const result = await api("/api/file", {
    method: "PUT",
    body: JSON.stringify({
      path: documentState.path,
      content: documentState.content,
    }),
  });

  state.documents.set(documentState.path, {
    ...documentState,
    savedContent: documentState.content,
    dirty: false,
    updatedAt: result.updatedAt,
    size: result.size,
    externallyModified: false,
  });

  await refreshParentOf(documentState.path);
  renderEditor();
  setNotice(`Saved ${documentState.path}.`);
}

function revertActiveDocument() {
  const documentState = getActiveDocument();

  if (!documentState) {
    return;
  }

  state.documents.set(documentState.path, {
    ...documentState,
    content: documentState.savedContent,
    dirty: false,
    externallyModified: false,
  });
  renderEditor();
}

async function refreshParentOf(relativePath) {
  const parentPath = dirname(relativePath);
  await loadDirectory(parentPath);
}

async function createWorkspaceItem(kind) {
  const selectedKind = getKnownItemKind(state.explorerSelection);
  const seed =
    selectedKind === "directory"
      ? state.explorerSelection
      : state.explorerSelection
        ? dirname(state.explorerSelection)
        : state.activePath
          ? dirname(state.activePath)
          : "";
  const suggestion =
    kind === "directory"
      ? seed || "new-folder"
      : seed
        ? `${seed}/new-file.txt`
        : "new-file.txt";
  const requestedPath = window.prompt(`Create ${kind}:`, suggestion);

  if (!requestedPath) {
    return;
  }

  await api("/api/fs/item", {
    method: "POST",
    body: JSON.stringify({
      path: requestedPath.trim(),
      kind,
      content: kind === "file" ? "" : undefined,
    }),
  });

  await ensureParentDirectories(requestedPath.trim());
  await refreshExpandedDirectories();
  setNotice(`Created ${requestedPath.trim()}.`);

  if (kind === "file") {
    await openFile(requestedPath.trim(), {
      forceReload: true,
    });
  } else {
    state.explorerSelection = requestedPath.trim();
    renderExplorer();
  }
}

async function renameSelectedItem() {
  const target = state.explorerSelection || state.activePath;

  if (!target) {
    window.alert("Select a file or folder first.");
    return;
  }

  const nextPath = window.prompt("Rename path:", target);

  if (!nextPath || nextPath === target) {
    return;
  }

  await api("/api/fs/rename", {
    method: "POST",
    body: JSON.stringify({
      fromPath: target,
      toPath: nextPath.trim(),
    }),
  });

  const renamedPath = nextPath.trim();

  for (const [path, documentState] of state.documents.entries()) {
    if (path === target || path.startsWith(`${target}/`)) {
      const suffix = path === target ? "" : path.slice(target.length);
      state.documents.delete(path);
      state.documents.set(`${renamedPath}${suffix}`, {
        ...documentState,
        path: `${renamedPath}${suffix}`,
      });
    }
  }

  state.openTabs = state.openTabs.map((path) =>
    path === target || path.startsWith(`${target}/`)
      ? `${renamedPath}${path === target ? "" : path.slice(target.length)}`
      : path,
  );

  if (state.activePath === target || state.activePath.startsWith(`${target}/`)) {
    state.activePath =
      state.activePath === target
        ? renamedPath
        : `${renamedPath}${state.activePath.slice(target.length)}`;
  }

  state.explorerSelection = renamedPath;
  await ensureParentDirectories(renamedPath);
  await refreshExpandedDirectories();
  renderTabs();
  renderEditor();
  setNotice(`Renamed ${target} to ${renamedPath}.`);
}

async function deleteSelectedItem() {
  const target = state.explorerSelection || state.activePath;

  if (!target) {
    window.alert("Select a file or folder first.");
    return;
  }

  if (!window.confirm(`Delete ${target}? This cannot be undone.`)) {
    return;
  }

  await api(`/api/fs?path=${encodeURIComponent(target)}`, {
    method: "DELETE",
  });

  for (const path of Array.from(state.documents.keys())) {
    if (path === target || path.startsWith(`${target}/`)) {
      state.documents.delete(path);
    }
  }

  state.openTabs = state.openTabs.filter(
    (path) => path !== target && !path.startsWith(`${target}/`),
  );

  if (state.activePath === target || state.activePath.startsWith(`${target}/`)) {
    state.activePath = state.openTabs[state.openTabs.length - 1] || "";
  }

  state.explorerSelection = dirname(target);
  await refreshExpandedDirectories();
  renderTabs();
  renderEditor();
  setNotice(`Deleted ${target}.`);
}

function renderTerminal() {
  dom.terminalOutput.innerHTML = state.terminal.events.length
    ? state.terminal.events
        .map((event) => {
          const content = stripAnsi(event.data || event.message || "");

          return `
            <div class="terminal-event">
              <div class="terminal-meta">${escapeHtml(formatTimestamp(event.at))} • ${escapeHtml(event.type)}</div>
              <div class="terminal-chunk">${escapeHtml(content)}</div>
            </div>
          `;
        })
        .join("")
    : `<div class="terminal-event"><div class="terminal-meta">Terminal not started yet.</div></div>`;

  dom.terminalOutput.scrollTop = dom.terminalOutput.scrollHeight;
  renderHeader();
}

async function createTerminalSession() {
  if (state.terminal.source) {
    state.terminal.source.close();
    state.terminal.source = null;
  }

  const session = await api("/api/terminal/session", {
    method: "POST",
    body: JSON.stringify({
      cwd: "",
    }),
  });

  state.terminal.sessionId = session.id;
  state.terminal.status = session.status;
  state.terminal.events = [];
  connectTerminalStream(session.id);
  renderTerminal();
}

function connectTerminalStream(sessionId) {
  if (state.terminal.source) {
    state.terminal.source.close();
  }

  const source = new EventSource(`/api/terminal/stream?sessionId=${encodeURIComponent(sessionId)}`);
  state.terminal.source = source;

  source.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    state.terminal.events.push(payload);
    if (state.terminal.events.length > 500) {
      state.terminal.events.shift();
    }

    if (payload.type === "exit") {
      state.terminal.status = "exited";
    }

    renderTerminal();
  };

  source.onerror = () => {
    state.terminal.status = "disconnected";
    renderTerminal();
  };
}

async function sendTerminalInput() {
  const input = dom.terminalInput.value;

  if (!input.trim() || !state.terminal.sessionId) {
    return;
  }

  await api("/api/terminal/input", {
    method: "POST",
    body: JSON.stringify({
      sessionId: state.terminal.sessionId,
      input: `${input}\n`,
    }),
  });

  dom.terminalInput.value = "";
}

function getAgentProviderPayload() {
  const { apiKey = "", baseUrl = "", model = "" } = state.agent.config;

  if (apiKey && baseUrl && model) {
    return {
      apiKey,
      baseUrl,
      model,
    };
  }

  return null;
}

function renderQuickPrompts() {
  dom.quickPrompts.innerHTML = QUICK_PROMPTS.map(
    (entry) => `
      <button class="quick-prompt" data-quick-prompt="${escapeAttribute(entry.prompt)}" type="button">
        <span class="quick-title">${escapeHtml(entry.title)}</span>
        <span class="quick-body">${escapeHtml(entry.body)}</span>
      </button>
    `,
  ).join("");
}

function renderAgentStream() {
  dom.agentStream.innerHTML = state.agent.events.length
    ? state.agent.events
        .map((event) => {
          const label = event.type.replaceAll("-", " ");
          const content =
            event.content ||
            event.message ||
            event.detail ||
            (event.result ? JSON.stringify(event.result, null, 2) : "") ||
            (event.input ? JSON.stringify(event.input, null, 2) : "");

          return `
            <article class="agent-event ${escapeAttribute(event.type)}">
              <div class="event-label">${escapeHtml(label)}</div>
              <div class="agent-meta">${escapeHtml(formatTimestamp(event.at))}</div>
              ${
                content
                  ? `<div class="agent-content">${escapeHtml(content)}</div>`
                  : ""
              }
              ${
                event.tool
                  ? `<div class="agent-meta">tool: ${escapeHtml(event.tool)}</div>`
                  : ""
              }
              ${
                event.status
                  ? `<div class="agent-meta">status: ${escapeHtml(event.status)}</div>`
                  : ""
              }
            </article>
          `;
        })
        .join("")
    : `<div class="agent-event muted">Agent output will stream here.</div>`;

  dom.agentStream.scrollTop = dom.agentStream.scrollHeight;
  renderHeader();
}

function applyAgentFormState() {
  dom.agentPrompt.value = state.agent.prompt;
  dom.agentCwdInput.value = state.agent.cwd;
  dom.agentBaseUrlInput.value = state.agent.config.baseUrl || "";
  dom.agentModelInput.value = state.agent.config.model || "";
  dom.agentApiKeyInput.value = "";
}

async function handleWorkspaceChange(relativePath) {
  await refreshParentOf(relativePath);

  const documentState = state.documents.get(relativePath);

  if (!documentState) {
    return;
  }

  if (documentState.dirty) {
    state.documents.set(relativePath, {
      ...documentState,
      externallyModified: true,
    });
    renderEditor();
    setNotice(`${relativePath} changed externally while you have unsaved edits.`);
    return;
  }

  await openFile(relativePath, {
    forceReload: true,
  });
}

function connectAgentStream(runId) {
  if (state.agent.source) {
    state.agent.source.close();
  }

  const source = new EventSource(`/api/agent/stream?runId=${encodeURIComponent(runId)}`);
  state.agent.source = source;

  source.onmessage = async (event) => {
    try {
      const payload = JSON.parse(event.data);
      state.agent.events.push(payload);
      if (state.agent.events.length > 250) {
        state.agent.events.shift();
      }

      if (payload.type === "status") {
        state.agent.status = payload.status;
      }

      if (payload.type === "workspace-changed" && payload.path) {
        await handleWorkspaceChange(payload.path);
      }

      if (payload.type === "error") {
        state.agent.status = "failed";
        setNotice(payload.message);
      }

      if (payload.type === "final") {
        setNotice("Agent run completed.");
      }

      if (payload.type === "status" && ["completed", "failed"].includes(payload.status)) {
        state.agent.providerMode = payload.provider || state.agent.providerMode;
        source.close();
        state.agent.source = null;
      }

      renderAgentStream();
    } catch (error) {
      state.agent.status = "failed";
      setNotice(error.message);
      renderAgentStream();
    }
  };

  source.onerror = () => {
    if (state.agent.status === "running") {
      state.agent.status = "disconnected";
      renderAgentStream();
    }
  };
}

async function runAgent() {
  const prompt = dom.agentPrompt.value.trim();

  if (!prompt) {
    window.alert("Enter an agent prompt first.");
    return;
  }

  state.agent.prompt = prompt;
  state.agent.cwd = dom.agentCwdInput.value.trim();
  state.agent.config = {
    apiKey: dom.agentApiKeyInput.value.trim(),
    baseUrl: dom.agentBaseUrlInput.value.trim(),
    model: dom.agentModelInput.value.trim(),
  };
  saveAgentConfig();

  state.agent.events = [];
  state.agent.status = "running";
  renderAgentStream();
  setNotice("Starting agent run…");

  const run = await api("/api/agent/run", {
    method: "POST",
    body: JSON.stringify({
      prompt,
      cwd: state.agent.cwd,
      providerConfig: getAgentProviderPayload(),
    }),
  });

  state.agent.runId = run.id;
  state.agent.providerMode = run.provider;
  renderHeader();
  connectAgentStream(run.id);
}

function syncAgentConfigFromInputs() {
  state.agent.prompt = dom.agentPrompt.value;
  state.agent.cwd = dom.agentCwdInput.value;
  state.agent.config = {
    apiKey: dom.agentApiKeyInput.value,
    baseUrl: dom.agentBaseUrlInput.value,
    model: dom.agentModelInput.value,
  };
  saveAgentConfig();
}

function handleTreeClick(event) {
  const target = event.target.closest("[data-tree-path]");

  if (!target) {
    return;
  }

  const relativePath = target.dataset.treePath;
  const kind = target.dataset.treeKind;
  state.explorerSelection = relativePath;
  renderExplorer();

  if (kind === "directory") {
    toggleDirectory(relativePath).catch((error) => {
      setNotice(error.message);
    });
    return;
  }

  openFile(relativePath).catch((error) => {
    setNotice(error.message);
  });
}

function handleTabClick(event) {
  const closeTarget = event.target.closest("[data-close-path]");

  if (closeTarget) {
    event.stopPropagation();
    closeTab(closeTarget.dataset.closePath);
    return;
  }

  const tabTarget = event.target.closest("[data-tab-path]");

  if (!tabTarget) {
    return;
  }

  state.activePath = tabTarget.dataset.tabPath;
  state.explorerSelection = state.activePath;
  renderTabs();
  renderEditor();
}

function handleSearchClick(event) {
  const resultTarget = event.target.closest("[data-search-path]");

  if (!resultTarget) {
    return;
  }

  openFile(resultTarget.dataset.searchPath, {
    line: Number(resultTarget.dataset.searchLine),
  }).catch((error) => {
    setNotice(error.message);
  });
}

function handleQuickPromptClick(event) {
  const promptTarget = event.target.closest("[data-quick-prompt]");

  if (!promptTarget) {
    return;
  }

  dom.agentPrompt.value = promptTarget.dataset.quickPrompt;
  syncAgentConfigFromInputs();
}

function bindEvents() {
  dom.explorerModeButton.addEventListener("click", () => setSidebarMode("explorer"));
  dom.searchModeButton.addEventListener("click", () => setSidebarMode("search"));
  dom.treeRoot.addEventListener("click", handleTreeClick);
  dom.searchResults.addEventListener("click", handleSearchClick);
  dom.tabStrip.addEventListener("click", handleTabClick);
  dom.quickPrompts.addEventListener("click", handleQuickPromptClick);

  dom.refreshTreeButton.addEventListener("click", () => {
    refreshExpandedDirectories().catch((error) => {
      setNotice(error.message);
    });
  });
  dom.newFileButton.addEventListener("click", () => {
    createWorkspaceItem("file").catch((error) => {
      setNotice(error.message);
    });
  });
  dom.newFolderButton.addEventListener("click", () => {
    createWorkspaceItem("directory").catch((error) => {
      setNotice(error.message);
    });
  });
  dom.renameButton.addEventListener("click", () => {
    renameSelectedItem().catch((error) => {
      setNotice(error.message);
    });
  });
  dom.deleteButton.addEventListener("click", () => {
    deleteSelectedItem().catch((error) => {
      setNotice(error.message);
    });
  });

  dom.runSearchButton.addEventListener("click", () => {
    runSearch().catch((error) => {
      setNotice(error.message);
    });
  });
  dom.searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      runSearch().catch((error) => {
        setNotice(error.message);
      });
    }
  });

  dom.editorTextarea.addEventListener("input", () => {
    const documentState = getActiveDocument();

    if (!documentState) {
      return;
    }

    const content = dom.editorTextarea.value;
    state.documents.set(documentState.path, {
      ...documentState,
      content,
      dirty: content !== documentState.savedContent,
    });
    dom.editorGutter.textContent = buildLineNumberMarkup(content);
    renderHeader();
    renderTabs();
  });

  dom.editorTextarea.addEventListener("scroll", () => {
    dom.editorGutter.scrollTop = dom.editorTextarea.scrollTop;
  });

  dom.saveButton.addEventListener("click", () => {
    saveActiveDocument().catch((error) => {
      setNotice(error.message);
    });
  });

  dom.revertButton.addEventListener("click", () => {
    revertActiveDocument();
  });

  dom.sendTerminalButton.addEventListener("click", () => {
    sendTerminalInput().catch((error) => {
      setNotice(error.message);
    });
  });

  dom.terminalInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendTerminalInput().catch((error) => {
        setNotice(error.message);
      });
    }
  });

  dom.clearTerminalButton.addEventListener("click", () => {
    state.terminal.events = [];
    renderTerminal();
  });

  dom.restartTerminalButton.addEventListener("click", () => {
    createTerminalSession().catch((error) => {
      setNotice(error.message);
    });
  });

  dom.runAgentButton.addEventListener("click", () => {
    runAgent().catch((error) => {
      setNotice(error.message);
      state.agent.status = "failed";
      renderAgentStream();
    });
  });

  dom.clearAgentButton.addEventListener("click", () => {
    state.agent.events = [];
    renderAgentStream();
  });

  for (const field of [
    dom.agentPrompt,
    dom.agentCwdInput,
    dom.agentBaseUrlInput,
    dom.agentModelInput,
    dom.agentApiKeyInput,
  ]) {
    field.addEventListener("input", syncAgentConfigFromInputs);
  }

  window.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveActiveDocument().catch((error) => {
        setNotice(error.message);
      });
    }

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "p") {
      event.preventDefault();
      setSidebarMode("search");
      dom.searchInput.focus();
      dom.searchInput.select();
    }
  });
}

async function boot() {
  bindEvents();
  renderQuickPrompts();
  applyAgentFormState();
  setSidebarMode("explorer");
  renderHeader();
  renderSearchResults();
  renderTerminal();
  renderAgentStream();

  state.config = await api("/api/config");
  state.agent.providerMode = state.config.providerMode;
  renderHeader();

  await loadDirectory("");
  await createTerminalSession();

  if (state.tree.get("")?.items?.some((item) => item.path === "README.md")) {
    await openFile("README.md");
  } else if (state.tree.get("")?.items?.some((item) => item.path === "server.js")) {
    await openFile("server.js");
  }

  setNotice("Workspace ready.");
}

boot().catch((error) => {
  setNotice(error.message);
});
