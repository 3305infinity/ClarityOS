import { spawn } from "node:child_process";
import os from "node:os";

const sessions = new Map();
let nextSessionId = 1;

function pushEvent(session, event) {
  const payload = {
    at: new Date().toISOString(),
    ...event,
  };

  session.events.push(payload);

  if (session.events.length > 400) {
    session.events.shift();
  }

  for (const client of session.clients) {
    client.write(`data: ${JSON.stringify(payload)}\n\n`);
  }
}

function createShell(cwd) {
  const shell = process.env.SHELL || (process.platform === "win32" ? "cmd.exe" : "sh");
  const args = process.platform === "win32" ? [] : ["-i"];

  return spawn(shell, args, {
    cwd,
    env: {
      ...process.env,
      TERM: "xterm-256color",
      FORCE_COLOR: "1",
    },
    stdio: "pipe",
  });
}

export function createTerminalManager({ resolveWorkspacePath }) {
  function createSession(relativeCwd = "") {
    const cwd = resolveWorkspacePath(relativeCwd);
    const child = createShell(cwd);
    const session = {
      id: String(nextSessionId++),
      cwd,
      child,
      createdAt: new Date().toISOString(),
      clients: new Set(),
      events: [],
      status: "running",
    };

    sessions.set(session.id, session);

    pushEvent(session, {
      type: "meta",
      message: `Shell started in ${cwd}`,
      sessionId: session.id,
      platform: os.platform(),
    });

    child.stdout.on("data", (chunk) => {
      pushEvent(session, {
        type: "stdout",
        data: chunk.toString(),
      });
    });

    child.stderr.on("data", (chunk) => {
      pushEvent(session, {
        type: "stderr",
        data: chunk.toString(),
      });
    });

    child.on("exit", (code, signal) => {
      session.status = "exited";
      pushEvent(session, {
        type: "exit",
        code,
        signal,
        message: `Shell exited${code !== null ? ` with code ${code}` : ""}.`,
      });
    });

    return {
      id: session.id,
      cwd: relativeCwd,
      status: session.status,
      createdAt: session.createdAt,
    };
  }

  function getSession(sessionId) {
    const session = sessions.get(String(sessionId));

    if (!session) {
      throw new Error(`Terminal session not found: ${sessionId}`);
    }

    return session;
  }

  function attachStream(sessionId, req, res) {
    const session = getSession(sessionId);

    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    });

    res.write("\n");
    session.clients.add(res);

    for (const event of session.events) {
      res.write(`data: ${JSON.stringify(event)}\n\n`);
    }

    const heartbeat = setInterval(() => {
      res.write(": heartbeat\n\n");
    }, 15000);

    req.on("close", () => {
      clearInterval(heartbeat);
      session.clients.delete(res);
      res.end();
    });
  }

  function writeInput(sessionId, input) {
    const session = getSession(sessionId);

    if (session.status !== "running") {
      throw new Error(`Terminal session is not running: ${sessionId}`);
    }

    session.child.stdin.write(input);

    return {
      sessionId: session.id,
      accepted: true,
    };
  }

  function closeSession(sessionId) {
    const session = getSession(sessionId);

    if (session.status === "running") {
      session.child.kill("SIGTERM");
    }

    return {
      sessionId: session.id,
      status: session.status,
    };
  }

  return {
    attachStream,
    closeSession,
    createSession,
    writeInput,
  };
}
