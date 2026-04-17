#!/usr/bin/env node

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const PACKAGE_ROOT = path.resolve(__dirname, "..");
const PACKAGE_JSON_PATH = path.join(PACKAGE_ROOT, "package.json");
const PACKAGE_MANIFEST = JSON.parse(fs.readFileSync(PACKAGE_JSON_PATH, "utf8"));
const WORKSPACE_ENV = "ZOOM_MEETING_BOT_HOME";
const BOOTSTRAP_PYTHON_ENV = "ZOOM_MEETING_BOT_BOOTSTRAP_PYTHON";

function main() {
  const workspaceRoot = resolveWorkspaceRoot();
  fs.mkdirSync(workspaceRoot, { recursive: true });

  const pythonExe = ensureBootstrap(workspaceRoot);
  const result = spawnSync(
    pythonExe,
    ["-m", "zoom_meeting_bot_cli", ...process.argv.slice(2)],
    {
      cwd: PACKAGE_ROOT,
      env: buildRuntimeEnv(workspaceRoot),
      stdio: "inherit",
    }
  );

  if (typeof result.status === "number") {
    process.exit(result.status);
  }
  if (result.error) {
    printError(`zoom-meeting-bot 실행에 실패했습니다: ${result.error.message}`);
  }
  process.exit(1);
}

function resolveWorkspaceRoot() {
  const configured = (process.env[WORKSPACE_ENV] || "").trim();
  if (configured) {
    return path.resolve(configured);
  }
  if (process.platform === "win32") {
    const base = process.env.LOCALAPPDATA || process.env.APPDATA || os.homedir();
    return path.join(base, "zoom-meeting-bot");
  }
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support", "zoom-meeting-bot");
  }
  const base = process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(base, "zoom-meeting-bot");
}

function ensureBootstrap(workspaceRoot) {
  const bootstrapPython = resolveBootstrapPython();
  const venvPython = resolveVenvPython(workspaceRoot);
  const markerPath = path.join(workspaceRoot, ".tmp", "zoom-meeting-bot", "npm-bootstrap.json");

  if (!fs.existsSync(venvPython)) {
    fs.mkdirSync(path.dirname(markerPath), { recursive: true });
    printInfo(`가상환경을 준비합니다: ${workspaceRoot}`);
    runChecked(bootstrapPython.command, [...bootstrapPython.args, "-m", "venv", path.join(workspaceRoot, ".venv")], {
      cwd: workspaceRoot,
      env: buildRuntimeEnv(workspaceRoot),
      stdio: "inherit",
    });
  }

  const sourceFingerprint = readSourceFingerprint();
  const expectedMarker = {
    packageVersion: String(PACKAGE_MANIFEST.version || "").trim(),
    packageRoot: PACKAGE_ROOT,
    sourceFingerprint,
  };
  const currentMarker = readJson(markerPath);
  const needsInstall =
    !currentMarker ||
    currentMarker.packageVersion !== expectedMarker.packageVersion ||
    currentMarker.packageRoot !== expectedMarker.packageRoot ||
    currentMarker.sourceFingerprint !== expectedMarker.sourceFingerprint;

  if (needsInstall) {
    fs.mkdirSync(path.dirname(markerPath), { recursive: true });
    printInfo("Python 의존성과 CLI 진입점을 연결합니다.");
    runChecked(venvPython, ["-m", "pip", "install", "--upgrade", "pip", "setuptools<82", "wheel"], {
      cwd: PACKAGE_ROOT,
      env: buildRuntimeEnv(workspaceRoot),
      stdio: "inherit",
    });
    runChecked(venvPython, ["-m", "pip", "install", "-e", PACKAGE_ROOT], {
      cwd: PACKAGE_ROOT,
      env: buildRuntimeEnv(workspaceRoot),
      stdio: "inherit",
    });
    fs.writeFileSync(markerPath, JSON.stringify(expectedMarker, null, 2) + "\n", "utf8");
  }

  return venvPython;
}

function resolveBootstrapPython() {
  const configured = (process.env[BOOTSTRAP_PYTHON_ENV] || "").trim();
  if (configured && fs.existsSync(configured)) {
    return { command: configured, args: [] };
  }

  const localCandidates = process.platform === "win32"
    ? [path.join(PACKAGE_ROOT, ".venv", "Scripts", "python.exe")]
    : [path.join(PACKAGE_ROOT, ".venv", "bin", "python")];
  for (const candidate of localCandidates) {
    if (fs.existsSync(candidate)) {
      return { command: candidate, args: [] };
    }
  }

  const candidates = process.platform === "win32"
    ? [
        { command: "py", args: ["-3"] },
        { command: "python", args: [] },
        { command: "python3", args: [] },
      ]
    : [
        { command: "python3", args: [] },
        { command: "python", args: [] },
      ];

  for (const candidate of candidates) {
    const probe = spawnSync(candidate.command, [...candidate.args, "--version"], {
      stdio: "ignore",
    });
    if (!probe.error && probe.status === 0) {
      return candidate;
    }
  }

  printError("Python 3을 찾을 수 없습니다. Python 3.11 이상을 먼저 설치한 뒤 다시 실행해 주세요.");
  process.exit(1);
}

function resolveVenvPython(workspaceRoot) {
  if (process.platform === "win32") {
    return path.join(workspaceRoot, ".venv", "Scripts", "python.exe");
  }
  return path.join(workspaceRoot, ".venv", "bin", "python");
}

function buildRuntimeEnv(workspaceRoot) {
  const env = { ...process.env };
  env[WORKSPACE_ENV] = workspaceRoot;
  env.PYTHONUTF8 = "1";
  env.PYTHONIOENCODING = "utf-8";
  env.PIP_DISABLE_PIP_VERSION_CHECK = "1";
  env.PYTHONPATH = appendPath(path.join(PACKAGE_ROOT, "src"), env.PYTHONPATH || "");
  return env;
}

function appendPath(prefix, currentValue) {
  const value = String(currentValue || "").trim();
  if (!value) {
    return prefix;
  }
  const parts = value.split(path.delimiter).filter(Boolean);
  if (parts.includes(prefix)) {
    return value;
  }
  return [prefix, ...parts].join(path.delimiter);
}

function readSourceFingerprint() {
  try {
    const stat = fs.statSync(path.join(PACKAGE_ROOT, "pyproject.toml"));
    return String(stat.mtimeMs);
  } catch (_error) {
    return "missing-pyproject";
  }
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (_error) {
    return null;
  }
}

function runChecked(command, args, options) {
  const result = spawnSync(command, args, options);
  if (typeof result.status === "number" && result.status === 0) {
    return;
  }
  if (result.error) {
    printError(`${command} 실행에 실패했습니다: ${result.error.message}`);
  } else {
    printError(`${command} 실행이 실패했습니다. 종료 코드: ${result.status}`);
  }
  process.exit(typeof result.status === "number" ? result.status : 1);
}

function printInfo(message) {
  process.stderr.write(`[zoom-meeting-bot] ${message}\n`);
}

function printError(message) {
  process.stderr.write(`[zoom-meeting-bot] ${message}\n`);
}

main();
