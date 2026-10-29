import fs from "node:fs/promises";
import path from "node:path";

export const WORKSPACE_ROOT = process.cwd();

const SEARCH_IGNORES = new Set([
  ".git",
  ".DS_Store",
  "node_modules",
  "dist",
  "build",
  "coverage",
  ".next",
  ".turbo",
  ".idea",
]);

const MAX_TEXT_FILE_SIZE = 1024 * 1024 * 2;
const MAX_SEARCH_FILE_SIZE = 1024 * 512;

function toPosixPath(value) {
  return value.split(path.sep).join("/");
}

export function normalizeRelativePath(relativePath = "") {
  if (!relativePath || relativePath === ".") {
    return "";
  }

  return toPosixPath(path.posix.normalize(relativePath)).replace(/^\/+/, "");
}

export function resolveWorkspacePath(relativePath = "") {
  const normalized = normalizeRelativePath(relativePath);
  const absolutePath = path.resolve(WORKSPACE_ROOT, normalized);
  const relativeFromRoot = path.relative(WORKSPACE_ROOT, absolutePath);

  if (
    relativeFromRoot.startsWith("..") ||
    path.isAbsolute(relativeFromRoot)
  ) {
    throw new Error("Path escapes workspace root.");
  }

  return absolutePath;
}

async function statSafe(absolutePath) {
  try {
    return await fs.stat(absolutePath);
  } catch {
    return null;
  }
}

function compareEntries(left, right) {
  if (left.kind !== right.kind) {
    return left.kind === "directory" ? -1 : 1;
  }

  return left.name.localeCompare(right.name);
}

export async function listDirectory(relativePath = "") {
  const normalized = normalizeRelativePath(relativePath);
  const absolutePath = resolveWorkspacePath(normalized);
  const stat = await statSafe(absolutePath);

  if (!stat) {
    throw new Error(`Directory does not exist: ${normalized || "."}`);
  }

  if (!stat.isDirectory()) {
    throw new Error(`Path is not a directory: ${normalized || "."}`);
  }

  const entries = await fs.readdir(absolutePath, { withFileTypes: true });
  const items = await Promise.all(
    entries.map(async (entry) => {
      const childRelativePath = normalizeRelativePath(
        path.posix.join(normalized, entry.name),
      );
      const childAbsolutePath = resolveWorkspacePath(childRelativePath);
      const childStat = await fs.stat(childAbsolutePath);

      return {
        name: entry.name,
        path: childRelativePath,
        kind: entry.isDirectory() ? "directory" : "file",
        size: childStat.size,
        updatedAt: childStat.mtime.toISOString(),
      };
    }),
  );

  items.sort(compareEntries);

  return {
    path: normalized,
    items,
  };
}

function isBinaryContent(content) {
  return content.includes("\u0000");
}

export async function readWorkspaceFile(relativePath) {
  const normalized = normalizeRelativePath(relativePath);

  if (!normalized) {
    throw new Error("A file path is required.");
  }

  const absolutePath = resolveWorkspacePath(normalized);
  const stat = await statSafe(absolutePath);

  if (!stat) {
    throw new Error(`File does not exist: ${normalized}`);
  }

  if (!stat.isFile()) {
    throw new Error(`Path is not a file: ${normalized}`);
  }

  if (stat.size > MAX_TEXT_FILE_SIZE) {
    throw new Error(`File is too large to open in the editor: ${normalized}`);
  }

  const content = await fs.readFile(absolutePath, "utf8");

  if (isBinaryContent(content)) {
    throw new Error(`Binary files are not supported in the editor: ${normalized}`);
  }

  return {
    path: normalized,
    content,
    size: stat.size,
    updatedAt: stat.mtime.toISOString(),
  };
}

export async function writeWorkspaceFile(relativePath, content = "") {
  const normalized = normalizeRelativePath(relativePath);

  if (!normalized) {
    throw new Error("A file path is required.");
  }

  const absolutePath = resolveWorkspacePath(normalized);
  await fs.mkdir(path.dirname(absolutePath), { recursive: true });
  await fs.writeFile(absolutePath, content, "utf8");
  const stat = await fs.stat(absolutePath);

  return {
    path: normalized,
    size: stat.size,
    updatedAt: stat.mtime.toISOString(),
  };
}

export async function createWorkspaceItem({
  path: relativePath,
  kind,
  content = "",
}) {
  const normalized = normalizeRelativePath(relativePath);

  if (!normalized) {
    throw new Error("A path is required.");
  }

  const absolutePath = resolveWorkspacePath(normalized);
  const existing = await statSafe(absolutePath);

  if (existing) {
    throw new Error(`Path already exists: ${normalized}`);
  }

  if (kind === "directory") {
    await fs.mkdir(absolutePath, { recursive: true });
  } else if (kind === "file") {
    await fs.mkdir(path.dirname(absolutePath), { recursive: true });
    await fs.writeFile(absolutePath, content, "utf8");
  } else {
    throw new Error(`Unsupported workspace item kind: ${kind}`);
  }

  return {
    path: normalized,
    kind,
  };
}

export async function renameWorkspaceItem(fromPath, toPath) {
  const normalizedFrom = normalizeRelativePath(fromPath);
  const normalizedTo = normalizeRelativePath(toPath);

  if (!normalizedFrom || !normalizedTo) {
    throw new Error("Both source and destination paths are required.");
  }

  const absoluteFrom = resolveWorkspacePath(normalizedFrom);
  const absoluteTo = resolveWorkspacePath(normalizedTo);
  const sourceStat = await statSafe(absoluteFrom);

  if (!sourceStat) {
    throw new Error(`Path does not exist: ${normalizedFrom}`);
  }

  if (await statSafe(absoluteTo)) {
    throw new Error(`Destination already exists: ${normalizedTo}`);
  }

  await fs.mkdir(path.dirname(absoluteTo), { recursive: true });
  await fs.rename(absoluteFrom, absoluteTo);

  return {
    fromPath: normalizedFrom,
    toPath: normalizedTo,
  };
}

export async function deleteWorkspaceItem(relativePath) {
  const normalized = normalizeRelativePath(relativePath);

  if (!normalized) {
    throw new Error("A path is required.");
  }

  const absolutePath = resolveWorkspacePath(normalized);
  const targetStat = await statSafe(absolutePath);

  if (!targetStat) {
    throw new Error(`Path does not exist: ${normalized}`);
  }

  await fs.rm(absolutePath, {
    recursive: targetStat.isDirectory(),
    force: false,
  });

  return {
    path: normalized,
    kind: targetStat.isDirectory() ? "directory" : "file",
  };
}

function shouldIgnoreEntry(name) {
  return SEARCH_IGNORES.has(name);
}

function buildSearchSnippet(line, query) {
  const lowerLine = line.toLowerCase();
  const lowerQuery = query.toLowerCase();
  const matchIndex = lowerLine.indexOf(lowerQuery);

  if (matchIndex === -1) {
    return line.trim().slice(0, 160);
  }

  const sliceStart = Math.max(0, matchIndex - 48);
  const sliceEnd = Math.min(line.length, matchIndex + query.length + 72);
  return line.slice(sliceStart, sliceEnd).trim();
}

export async function searchWorkspace(
  query,
  relativePath = "",
  limit = 100,
) {
  const normalizedQuery = `${query || ""}`.trim();

  if (!normalizedQuery) {
    return [];
  }

  const normalizedRoot = normalizeRelativePath(relativePath);
  const rootAbsolutePath = resolveWorkspacePath(normalizedRoot);
  const results = [];

  async function walk(currentAbsolutePath, currentRelativePath) {
    if (results.length >= limit) {
      return;
    }

    const stat = await statSafe(currentAbsolutePath);

    if (!stat) {
      return;
    }

    if (stat.isDirectory()) {
      const entries = await fs.readdir(currentAbsolutePath, {
        withFileTypes: true,
      });

      for (const entry of entries) {
        if (results.length >= limit) {
          break;
        }

        if (shouldIgnoreEntry(entry.name)) {
          continue;
        }

        const childRelativePath = normalizeRelativePath(
          path.posix.join(currentRelativePath, entry.name),
        );
        const childAbsolutePath = resolveWorkspacePath(childRelativePath);
        await walk(childAbsolutePath, childRelativePath);
      }

      return;
    }

    if (!stat.isFile() || stat.size > MAX_SEARCH_FILE_SIZE) {
      return;
    }

    let content = "";

    try {
      content = await fs.readFile(currentAbsolutePath, "utf8");
    } catch {
      return;
    }

    if (isBinaryContent(content)) {
      return;
    }

    const lines = content.split(/\r?\n/u);

    for (let index = 0; index < lines.length; index += 1) {
      const line = lines[index];

      if (line.toLowerCase().includes(normalizedQuery.toLowerCase())) {
        results.push({
          path: currentRelativePath,
          line: index + 1,
          snippet: buildSearchSnippet(line, normalizedQuery),
        });
      }

      if (results.length >= limit) {
        break;
      }
    }
  }

  await walk(rootAbsolutePath, normalizedRoot);

  return results;
}

export async function summarizeWorkspace(relativePath = "") {
  const { items } = await listDirectory(relativePath);
  const directories = items.filter((item) => item.kind === "directory");
  const files = items.filter((item) => item.kind === "file");

  return {
    root: normalizeRelativePath(relativePath),
    directories: directories.map((entry) => entry.path),
    files: files.map((entry) => entry.path),
    counts: {
      directories: directories.length,
      files: files.length,
    },
  };
}
