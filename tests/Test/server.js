import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { createAgentManager } from "./lib/agent.js";
import { createTerminalManager } from "./lib/terminal.js";
import {
  WORKSPACE_ROOT,
  createWorkspaceItem,
  deleteWorkspaceItem,
  listDirectory,
  normalizeRelativePath,
  readWorkspaceFile,
  renameWorkspaceItem,
  resolveWorkspacePath,
  searchWorkspace,
  summarizeWorkspace,
  writeWorkspaceFile,
} from "./lib/workspace.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PUBLIC_ROOT = path.join(__dirname, "public");

const agentManager = createAgentManager({
  createWorkspaceItem,
  listDirectory,
  readWorkspaceFile,
  resolveWorkspacePath,
  searchWorkspace,
  summarizeWorkspace,
  writeWorkspaceFile,
});

const terminalManager = createTerminalManager({
  resolveWorkspacePath,
});

function json(res, statusCode, payload) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
  });
  res.end(JSON.stringify(payload));
}

function sendError(res, statusCode, error) {
  json(res, statusCode, {
    error: error.message || "Unexpected server error.",
  });
}

function getErrorStatusCode(error) {
  const message = `${error?.message || ""}`;

  if (error instanceof SyntaxError) {
    return 400;
  }

  if (error?.code === "ENOENT") {
    return 404;
  }

  if (
    message.includes("not found") ||
    message.includes("does not exist")
  ) {
    return 404;
  }

  return 400;
}

async function readRequestBody(req) {
  const chunks = [];

  for await (const chunk of req) {
    chunks.push(chunk);
  }

  if (!chunks.length) {
    return {};
  }

  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function getMimeType(filePath) {
  if (filePath.endsWith(".css")) {
    return "text/css; charset=utf-8";
  }

  if (filePath.endsWith(".js")) {
    return "application/javascript; charset=utf-8";
  }

  if (filePath.endsWith(".json")) {
    return "application/json; charset=utf-8";
  }

  if (filePath.endsWith(".svg")) {
    return "image/svg+xml";
  }

  if (filePath.endsWith(".png")) {
    return "image/png";
  }

  return "text/html; charset=utf-8";
}

async function serveStaticAsset(res, pathname) {
  const safePath = pathname === "/" ? "/index.html" : pathname;
  const assetPath = path.join(PUBLIC_ROOT, safePath);
  const relativeToPublic = path.relative(PUBLIC_ROOT, assetPath);

  if (relativeToPublic.startsWith("..") || path.isAbsolute(relativeToPublic)) {
    throw new Error("Invalid asset path.");
  }

  const content = await fs.readFile(assetPath);

  res.writeHead(200, {
    "Content-Type": getMimeType(assetPath),
  });
  res.end(content);
}

function createServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");

    try {
      if (url.pathname === "/api/health" && req.method === "GET") {
        json(res, 200, {
          ok: true,
          workspaceRoot: WORKSPACE_ROOT,
        });
        return;
      }

      if (url.pathname === "/api/config" && req.method === "GET") {
        json(res, 200, {
          workspaceRoot: WORKSPACE_ROOT,
          providerMode: agentManager.getDefaultProviderMode(),
          port: Number(process.env.PORT || 4173),
        });
        return;
      }

      if (url.pathname === "/api/tree" && req.method === "GET") {
        const targetPath = normalizeRelativePath(url.searchParams.get("path") || "");
        json(res, 200, await listDirectory(targetPath));
        return;
      }

      if (url.pathname === "/api/file" && req.method === "GET") {
        const targetPath = normalizeRelativePath(url.searchParams.get("path") || "");
        json(res, 200, await readWorkspaceFile(targetPath));
        return;
      }

      if (url.pathname === "/api/file" && req.method === "PUT") {
        const body = await readRequestBody(req);
        json(res, 200, await writeWorkspaceFile(body.path, body.content ?? ""));
        return;
      }

      if (url.pathname === "/api/fs/item" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, await createWorkspaceItem(body));
        return;
      }

      if (url.pathname === "/api/fs/rename" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, await renameWorkspaceItem(body.fromPath, body.toPath));
        return;
      }

      if (url.pathname === "/api/fs" && req.method === "DELETE") {
        const targetPath = normalizeRelativePath(url.searchParams.get("path") || "");
        json(res, 200, await deleteWorkspaceItem(targetPath));
        return;
      }

      if (url.pathname === "/api/search" && req.method === "GET") {
        const query = url.searchParams.get("q") || "";
        const targetPath = normalizeRelativePath(url.searchParams.get("path") || "");
        json(res, 200, await searchWorkspace(query, targetPath, 100));
        return;
      }

      if (url.pathname === "/api/terminal/session" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, terminalManager.createSession(body.cwd || ""));
        return;
      }

      if (url.pathname === "/api/terminal/stream" && req.method === "GET") {
        const sessionId = url.searchParams.get("sessionId");
        terminalManager.attachStream(sessionId, req, res);
        return;
      }

      if (url.pathname === "/api/terminal/input" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, terminalManager.writeInput(body.sessionId, body.input ?? ""));
        return;
      }

      if (url.pathname === "/api/terminal/close" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, terminalManager.closeSession(body.sessionId));
        return;
      }

      if (url.pathname === "/api/agent/run" && req.method === "POST") {
        const body = await readRequestBody(req);
        json(res, 200, agentManager.createRun(body));
        return;
      }

      if (url.pathname === "/api/agent/stream" && req.method === "GET") {
        const runId = url.searchParams.get("runId");
        agentManager.attachStream(runId, req, res);
        return;
      }

      if (req.method === "GET") {
        await serveStaticAsset(res, url.pathname);
        return;
      }

      sendError(res, 404, new Error("Route not found."));
    } catch (error) {
      sendError(res, getErrorStatusCode(error), error);
    }
  });
}

const port = Number(process.env.PORT || 4173);

if (process.argv[1] === __filename) {
  const server = createServer();
  server.listen(port, () => {
    console.log(`Moonwalk IDE listening on http://localhost:${port}`);
  });
}

export { createServer };
