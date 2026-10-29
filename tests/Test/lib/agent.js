import { spawn } from "node:child_process";

function createEmitter() {
  const clients = new Set();
  const events = [];

  function broadcast(event) {
    const payload = {
      at: new Date().toISOString(),
      ...event,
    };

    events.push(payload);

    if (events.length > 500) {
      events.shift();
    }

    for (const client of clients) {
      client.write(`data: ${JSON.stringify(payload)}\n\n`);
    }
  }

  function attach(req, res) {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });

    res.write("\n");
    clients.add(res);

    for (const event of events) {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    }

    const heartbeat = setInterval(() => {
      res.write(": heartbeat\n\n");
    }, 15000);

    req.on("close", () => {
      clearInterval(heartbeat);
      clients.delete(res);
      res.end();
    });
  }

  return {
    attach,
    broadcast,
  };
}

function formatContent(content) {
  if (typeof content === "string") {
    return content;
  }

  if (Array.isArray(content)) {
    return content
      .map((entry) => {
        if (typeof entry === "string") {
          return entry;
        }

        if (entry?.type === "text" && typeof entry.text === "string") {
          return entry.text;
        }

        return JSON.stringify(entry);
      })
      .join("\n");
  }

  return JSON.stringify(content, null, 2);
}

function parseJsonArguments(argumentText) {
  try {
    return JSON.parse(argumentText || "{}");
  } catch (error) {
    throw new Error(`Tool arguments are not valid JSON: ${error.message}`);
  }
}

function trimOutput(output, maxLength = 12000) {
  if (output.length <= maxLength) {
    return output;
  }

  return `${output.slice(0, maxLength)}\n\n[output truncated]`;
}

function runCommandCapture(command, cwd) {
  return new Promise((resolve, reject) => {
    const shell = process.env.SHELL || (process.platform === "win32" ? "cmd.exe" : "sh");
    const args = process.platform === "win32" ? ["/c", command] : ["-lc", command];
    const child = spawn(shell, args, {
      cwd,
      env: {
        ...process.env,
        TERM: "xterm-256color",
      },
    });

    const chunks = [];
    let timedOut = false;

    const timeout = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, 20000);

    child.stdout.on("data", (chunk) => {
      chunks.push(chunk.toString());
    });

    child.stderr.on("data", (chunk) => {
      chunks.push(chunk.toString());
    });

    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });

    child.on("close", (code, signal) => {
      clearTimeout(timeout);

      resolve({
        code,
        signal,
        timedOut,
        output: trimOutput(chunks.join("")),
      });
    });
  });
}

function buildToolDefinitions() {
  return [
    {
      type: "function",
      function: {
        name: "list_files",
        description: "List files and directories in a workspace directory.",
        parameters: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "Relative path inside the workspace.",
            },
          },
        },
      },
    },
    {
      type: "function",
      function: {
        name: "read_file",
        description: "Read a UTF-8 text file from the workspace.",
        parameters: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "Relative file path inside the workspace.",
            },
          },
          required: ["path"],
        },
      },
    },
    {
      type: "function",
      function: {
        name: "write_file",
        description: "Write a UTF-8 text file inside the workspace.",
        parameters: {
          type: "object",
          properties: {
            path: {
              type: "string",
              description: "Relative file path inside the workspace.",
            },
            content: {
              type: "string",
              description: "Full file content to write.",
            },
          },
          required: ["path", "content"],
        },
      },
    },
    {
      type: "function",
      function: {
        name: "search_workspace",
        description: "Search text in workspace files.",
        parameters: {
          type: "object",
          properties: {
            query: {
              type: "string",
              description: "Query text to search for.",
            },
            path: {
              type: "string",
              description: "Optional relative directory path to limit search.",
            },
          },
          required: ["query"],
        },
      },
    },
    {
      type: "function",
      function: {
        name: "run_command",
        description: "Run a shell command inside the workspace and capture its output.",
        parameters: {
          type: "object",
          properties: {
            command: {
              type: "string",
              description: "Shell command to run.",
            },
            cwd: {
              type: "string",
              description: "Optional relative workspace directory.",
            },
          },
          required: ["command"],
        },
      },
    },
  ];
}

function buildLocalHelpText() {
  return [
    "The local planner is active.",
    "Use slash-style commands for deterministic actions:",
    "/analyze",
    "/search <query>",
    "/read <path>",
    "/run <command>",
    "/write <path> then put the file content on following lines",
    "/mkdir <path>",
    "",
    "For open-ended autonomous editing, provide an OpenAI-compatible endpoint, model, and API key in the Agent panel.",
  ].join("\n");
}

function parseLocalCommand(prompt) {
  const [firstLine, ...remainingLines] = prompt.trim().split(/\r?\n/u);

  if (!firstLine) {
    return null;
  }

  const [command, ...rest] = firstLine.trim().split(/\s+/u);
  const argument = rest.join(" ").trim();

  return {
    command,
    argument,
    body: remainingLines.join("\n"),
  };
}

function toToolText(result) {
  return typeof result === "string" ? result : JSON.stringify(result, null, 2);
}

async function executeLocalPlanner(context, run) {
  const { prompt, cwd, tools } = context;
  const parsed = parseLocalCommand(prompt);

  run.broadcast({
    type: "status",
    status: "running",
    provider: "local-planner",
  });

  if (!parsed || !parsed.command.startsWith("/")) {
    const summary = await tools.summarizeWorkspace(cwd);
    run.broadcast({
      type: "plan",
      title: "Local planner fallback",
      detail: "Inspect workspace and return deterministic guidance.",
    });
    run.broadcast({
      type: "tool",
      tool: "summarize_workspace",
      result: summary,
    });
    run.broadcast({
      type: "final",
      content: `${buildLocalHelpText()}\n\nTop-level snapshot:\n${toToolText(summary)}`,
    });
    run.broadcast({
      type: "status",
      status: "completed",
    });
    return;
  }

  run.broadcast({
    type: "plan",
    title: "Execute local command",
    detail: `Running ${parsed.command} against the workspace.`,
  });

  switch (parsed.command) {
    case "/analyze": {
      const summary = await tools.summarizeWorkspace(cwd);
      run.broadcast({
        type: "tool",
        tool: "summarize_workspace",
        result: summary,
      });
      run.broadcast({
        type: "final",
        content: `Workspace summary:\n${toToolText(summary)}`,
      });
      break;
    }
    case "/search": {
      const result = await tools.searchWorkspace(parsed.argument, cwd, 50);
      run.broadcast({
        type: "tool",
        tool: "search_workspace",
        result,
      });
      run.broadcast({
        type: "final",
        content: `Search results for "${parsed.argument}":\n${toToolText(result)}`,
      });
      break;
    }
    case "/read": {
      const result = await tools.readFile(parsed.argument);
      run.broadcast({
        type: "tool",
        tool: "read_file",
        result: {
          path: result.path,
          size: result.size,
        },
      });
      run.broadcast({
        type: "final",
        content: `Contents of ${result.path}:\n\n${result.content}`,
      });
      break;
    }
    case "/run": {
      const result = await tools.runCommand(parsed.argument, cwd);
      run.broadcast({
        type: "tool",
        tool: "run_command",
        result,
      });
      run.broadcast({
        type: "final",
        content: `Command result:\n${toToolText(result)}`,
      });
      break;
    }
    case "/write": {
      const result = await tools.writeFile(parsed.argument, parsed.body);
      run.broadcast({
        type: "tool",
        tool: "write_file",
        result,
      });
      run.broadcast({
        type: "workspace-changed",
        action: "write",
        path: result.path,
      });
      run.broadcast({
        type: "final",
        content: `Wrote ${result.path}.`,
      });
      break;
    }
    case "/mkdir": {
      const result = await tools.createDirectory(parsed.argument);
      run.broadcast({
        type: "tool",
        tool: "create_directory",
        result,
      });
      run.broadcast({
        type: "workspace-changed",
        action: "mkdir",
        path: result.path,
      });
      run.broadcast({
        type: "final",
        content: `Created directory ${result.path}.`,
      });
      break;
    }
    default: {
      run.broadcast({
        type: "final",
        content: buildLocalHelpText(),
      });
      break;
    }
  }

  run.broadcast({
    type: "status",
    status: "completed",
  });
}

async function callOpenAICompatible({
  apiKey,
  baseUrl,
  model,
  messages,
  tools,
}) {
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      messages,
      tools,
      tool_choice: "auto",
      temperature: 0.2,
    }),
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(
      `Provider request failed (${response.status}): ${trimOutput(errorText, 3000)}`,
    );
  }

  const payload = await response.json();
  return payload?.choices?.[0]?.message;
}

async function executeOpenAICompatible(context, run) {
  const { prompt, cwd, providerConfig, tools } = context;
  const toolDefinitions = buildToolDefinitions();
  const systemPrompt = [
    "You are an autonomous coding agent inside a local IDE.",
    "Work only inside the provided workspace.",
    "Be concise in user-facing responses.",
    "Use tools whenever inspection or edits are required.",
    "Write complete files when editing.",
    `Current relative working directory: ${cwd || "."}`,
  ].join(" ");

  const messages = [
    {
      role: "system",
      content: systemPrompt,
    },
    {
      role: "user",
      content: prompt,
    },
  ];

  run.broadcast({
    type: "status",
    status: "running",
    provider: "openai-compatible",
    model: providerConfig.model,
  });

  for (let step = 0; step < 8; step += 1) {
    run.broadcast({
      type: "message",
      role: "system",
      content: `Agent step ${step + 1}`,
    });

    const assistantMessage = await callOpenAICompatible({
      apiKey: providerConfig.apiKey,
      baseUrl: providerConfig.baseUrl,
      model: providerConfig.model,
      messages,
      tools: toolDefinitions,
    });

    if (!assistantMessage) {
      throw new Error("Provider returned no assistant message.");
    }

    const assistantContent = formatContent(assistantMessage.content);

    if (assistantContent.trim()) {
      run.broadcast({
        type: "message",
        role: "assistant",
        content: assistantContent,
      });
    }

    messages.push({
      role: "assistant",
      content: assistantMessage.content ?? "",
      tool_calls: assistantMessage.tool_calls ?? [],
    });

    const toolCalls = assistantMessage.tool_calls ?? [];

    if (!toolCalls.length) {
      run.broadcast({
        type: "final",
        content: assistantContent || "Task completed.",
      });
      run.broadcast({
        type: "status",
        status: "completed",
      });
      return;
    }

    for (const toolCall of toolCalls) {
      const toolName = toolCall.function?.name;
      const argumentsText = toolCall.function?.arguments || "{}";
      const parsedArguments = parseJsonArguments(argumentsText);
      let result;

      switch (toolName) {
        case "list_files":
          result = await tools.listFiles(parsedArguments.path || cwd);
          break;
        case "read_file":
          result = await tools.readFile(parsedArguments.path);
          break;
        case "write_file":
          result = await tools.writeFile(parsedArguments.path, parsedArguments.content);
          run.broadcast({
            type: "workspace-changed",
            action: "write",
            path: parsedArguments.path,
          });
          break;
        case "search_workspace":
          result = await tools.searchWorkspace(
            parsedArguments.query,
            parsedArguments.path || cwd,
            50,
          );
          break;
        case "run_command":
          result = await tools.runCommand(
            parsedArguments.command,
            parsedArguments.cwd || cwd,
          );
          break;
        default:
          throw new Error(`Unknown tool requested by provider: ${toolName}`);
      }

      run.broadcast({
        type: "tool",
        tool: toolName,
        input: parsedArguments,
        result,
      });

      messages.push({
        role: "tool",
        tool_call_id: toolCall.id,
        content: JSON.stringify(result),
      });
    }
  }

  throw new Error("Agent exceeded the maximum tool loop depth.");
}

export function createAgentManager({
  listDirectory,
  summarizeWorkspace,
  readWorkspaceFile,
  writeWorkspaceFile,
  createWorkspaceItem,
  searchWorkspace,
  resolveWorkspacePath,
}) {
  const runs = new Map();
  let nextRunId = 1;

  function buildProviderConfig(providerConfig) {
    if (providerConfig?.apiKey && providerConfig?.model && providerConfig?.baseUrl) {
      return {
        mode: "openai-compatible",
        apiKey: providerConfig.apiKey,
        baseUrl: providerConfig.baseUrl,
        model: providerConfig.model,
      };
    }

    if (
      process.env.OPENAI_API_KEY &&
      process.env.OPENAI_MODEL &&
      process.env.OPENAI_BASE_URL
    ) {
      return {
        mode: "openai-compatible",
        apiKey: process.env.OPENAI_API_KEY,
        baseUrl: process.env.OPENAI_BASE_URL,
        model: process.env.OPENAI_MODEL,
      };
    }

    return {
      mode: "local-planner",
    };
  }

  function attachStream(runId, req, res) {
    const run = runs.get(String(runId));

    if (!run) {
      throw new Error(`Agent run not found: ${runId}`);
    }

    run.emitter.attach(req, res);
  }

  function createRun({ prompt, cwd = "", providerConfig = null }) {
    const id = String(nextRunId++);
    const emitter = createEmitter();
    const run = {
      id,
      emitter,
      prompt,
      cwd,
    };

    runs.set(id, run);

    const resolvedProvider = buildProviderConfig(providerConfig);
    const toolset = {
      createDirectory: async (relativePath) =>
        createWorkspaceItem({ path: relativePath, kind: "directory" }),
      listFiles: async (relativePath) => listDirectory(relativePath),
      readFile: async (relativePath) => readWorkspaceFile(relativePath),
      runCommand: async (command, relativePath = "") =>
        runCommandCapture(command, resolveWorkspacePath(relativePath)),
      searchWorkspace: async (query, relativePath, limit) =>
        searchWorkspace(query, relativePath, limit),
      summarizeWorkspace: async (relativePath) => summarizeWorkspace(relativePath),
      writeFile: async (relativePath, content) =>
        writeWorkspaceFile(relativePath, content),
    };

    const context = {
      prompt,
      cwd,
      providerConfig: resolvedProvider,
      tools: toolset,
    };

    Promise.resolve()
      .then(async () => {
        if (resolvedProvider.mode === "openai-compatible") {
          await executeOpenAICompatible(context, {
            broadcast: emitter.broadcast,
          });
          return;
        }

        await executeLocalPlanner(context, {
          broadcast: emitter.broadcast,
        });
      })
      .catch((error) => {
        emitter.broadcast({
          type: "error",
          message: error.message,
        });
        emitter.broadcast({
          type: "status",
          status: "failed",
        });
      });

    return {
      id,
      provider: resolvedProvider.mode,
    };
  }

  return {
    attachStream,
    createRun,
    getDefaultProviderMode() {
      return buildProviderConfig(null).mode;
    },
  };
}
