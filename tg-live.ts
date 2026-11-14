/**
 * tg-live.ts - tiny observer for the tgmessage AI process.
 *
 * pi-subagents keeps live agent records in memory, so Telegram cannot read
 * them from the session file. This extension mirrors the records to a small
 * heartbeat file that `/live` reads while the pi process is running.
 */

import fs from "node:fs";
import path from "node:path";

export default function (pi: any) {
  const dir = process.env.TG_LIVE_STATE_DIR;
  if (!dir) return;

  const file = path.join(dir, `${process.pid}.json`);
  const startedAt = Date.now();
  const agents = new Map<string, any>();
  let cwd = process.cwd();
  let sessionId = "";
  let sessionFile = "";
  let sessionName = "";
  let model = "";
  let heartbeat: ReturnType<typeof setInterval> | undefined;

  function snapshot() {
    return {
      pid: process.pid,
      cwd,
      sessionId,
      sessionFile,
      sessionName,
      model,
      startedAt,
      updatedAt: Date.now() / 1000,
      agents: [...agents.values()].sort(
        (a, b) => (a.startedAt || 0) - (b.startedAt || 0),
      ),
    };
  }

  function write() {
    try {
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
      const tmp = `${file}.${process.pid}.tmp`;
      fs.writeFileSync(tmp, JSON.stringify(snapshot()), { mode: 0o600 });
      fs.renameSync(tmp, file);
    } catch {
      /* observation must never break the agent */
    }
  }

  function remove() {
    try {
      fs.rmSync(file, { force: true });
    } catch {
      /* ignore */
    }
  }

  function startHeartbeat() {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = setInterval(write, 3000);
    heartbeat.unref?.();
  }

  function stopHeartbeat() {
    if (heartbeat) clearInterval(heartbeat);
    heartbeat = undefined;
  }

  function upsert(id: string, patch: Record<string, any>) {
    if (!id) return;
    const now = Date.now();
    const current = agents.get(id) || {
      id,
      type: "",
      description: "",
      status: "queued",
      startedAt: now,
    };
    agents.set(id, { ...current, ...patch });
    if (agents.size > 60) {
      const drop = [...agents.values()]
        .sort((a, b) => (a.startedAt || 0) - (b.startedAt || 0))
        .slice(0, agents.size - 60);
      for (const item of drop) agents.delete(item.id);
    }
    write();
  }

  pi.on("session_start", async (_event: any, ctx: any) => {
    cwd = ctx.cwd || process.cwd();
    sessionId = ctx.sessionManager?.getSessionId?.() || "";
    sessionFile = ctx.sessionManager?.getSessionFile?.() || "";
    sessionName = ctx.sessionManager?.getSessionName?.() || "";
    model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "";
    agents.clear();
    write();
    startHeartbeat();
  });

  pi.on("session_info_changed", async (event: any) => {
    sessionName = event.name || "";
    write();
  });

  pi.on("session_shutdown", async () => {
    stopHeartbeat();
    remove();
  });

  process.on("exit", remove);

  pi.events?.on("subagents:created", (event: any) => {
    if (!event?.id) return;
    const current = agents.get(event.id);
    if (current) {
      // started can arrive before created for background spawns.
      upsert(event.id, {
        type: event.type || current.type || "",
        description: event.description || current.description || "",
      });
      return;
    }
    upsert(event.id, {
      type: event.type || "",
      description: event.description || "",
      status: "queued",
      startedAt: Date.now(),
      completedAt: undefined,
      durationMs: undefined,
    });
  });

  pi.events?.on("subagents:started", (event: any) => {
    if (!event?.id) return;
    const current = agents.get(event.id);
    upsert(event.id, {
      type: event.type || current?.type || "",
      description: event.description || current?.description || "",
      status: "running",
      startedAt: Date.now(),
      completedAt: undefined,
      durationMs: undefined,
    });
  });

  pi.events?.on("subagents:completed", (event: any) => {
    if (!event?.id) return;
    const current = agents.get(event.id);
    upsert(event.id, {
      type: event.type || current?.type || "",
      description: event.description || current?.description || "",
      status: event.status || "completed",
      completedAt: Date.now(),
      durationMs: event.durationMs,
    });
  });

  pi.events?.on("subagents:failed", (event: any) => {
    if (!event?.id) return;
    const current = agents.get(event.id);
    upsert(event.id, {
      type: event.type || current?.type || "",
      description: event.description || current?.description || "",
      status: event.status || "error",
      completedAt: Date.now(),
      durationMs: event.durationMs,
    });
  });

  pi.events?.on("subagents:usage", (event: any) => {
    if (!event?.id) return;
    if ((event.depth ?? 1) > 1) return;
    const current = agents.get(event.id);
    if (!current) {
      upsert(event.id, {
        type: event.type || "",
        description: event.description || "",
        status: "running",
        startedAt: Date.now(),
      });
      return;
    }
    if (event.model || event.thinking) {
      upsert(event.id, {
        model: event.model || current.model,
        thinking: event.thinking || current.thinking,
      });
    }
  });

  pi.events?.on("subagents:disposed", () => {
    agents.clear();
    write();
  });

  // Visible immediately, before session_start arrives.
  write();
  startHeartbeat();
}
