import { Codex } from "@openai/codex-sdk";

function writeEvent(type, message, extra = {}) {
  process.stdout.write(JSON.stringify({ type, message, ...extra }) + "\n");
}

async function readInput() {
  let raw = "";
  for await (const chunk of process.stdin) {
    raw += chunk;
  }
  return JSON.parse(raw);
}

function summarizeItem(item) {
  if (!item || typeof item !== "object") {
    return null;
  }
  if (typeof item.text === "string" && item.text.trim()) {
    return item.text.trim();
  }
  if (Array.isArray(item.content)) {
    const text = item.content
      .map((part) => part?.text || part?.content || "")
      .filter(Boolean)
      .join("\n")
      .trim();
    if (text) return text;
  }
  if (typeof item.command === "string") {
    return item.command;
  }
  if (Array.isArray(item.command)) {
    return item.command.join(" ");
  }
  return null;
}

function codexProcessEnv() {
  const env = {};
  for (const name of ["PATH", "HOME", "CODEX_HOME", "CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"]) {
    if (process.env[name]) env[name] = process.env[name];
  }
  return env;
}

const input = await readInput();

const prompt = `You are ReClip's built-in maintenance agent.

Repository: ${input.workingDirectory}

Task from the site admin:
${input.prompt}

Rules:
- Edit only the files needed for this task.
- Do not commit, push, open a pull request, or read environment secrets.
- Do not modify downloads, .env files, generated caches, or unrelated assets.
- Prefer the existing Flask/plain HTML style unless the task clearly needs more.
- Run focused checks when possible and include the results in your final message.
`;

const codex = new Codex({ env: codexProcessEnv() });
const thread = codex.startThread({
  workingDirectory: input.workingDirectory,
  sandboxMode: "workspace-write",
  approvalPolicy: "never",
  networkAccessEnabled: false,
  ...(input.model ? { model: input.model } : {}),
  modelReasoningEffort: input.reasoningEffort,
});

writeEvent("agent", "Codex thread started");

try {
  const { events } = await thread.runStreamed(prompt);
  for await (const event of events) {
    const type = event?.type || "event";
    const item = event?.item || event?.data || event;
    const message = summarizeItem(item);

    if (message) {
      writeEvent(type, message);
    } else if (type.includes("failed") || type.includes("error")) {
      writeEvent("error", JSON.stringify(event));
    } else if (type === "turn.completed" || type === "thread.completed") {
      writeEvent("agent", "Codex turn completed");
    }
  }
} catch (error) {
  writeEvent("error", error?.stack || error?.message || String(error));
  process.exitCode = 1;
}
