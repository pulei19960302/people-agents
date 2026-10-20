/**
 * 将 SSE 事件列表转换为时间线展示项与计划步骤
 * 与 chat 流式 / 任务详情接口的响应格式一致
 *
 * 后端事件格式为 { event: "message"|"title"|..., data: {...} }，
 * 前端统一使用 { type, data }，需先归一化。
 */

import type {
  SSEEventData,
  SSEEventType,
  ChatMessage,
  PlanStep,
  PlanEvent,
  StepEvent,
  ToolEvent,
  SessionFile,
} from "@/lib/api/types";

/** 后端返回的原始事件（可能用 event 或 type 表示类型） */
type RawEvent = { event?: string; type?: string; data?: unknown };

/**
 * 将后端单条事件转为前端 SSEEventData（统一 type + data）
 */
export function normalizeEvent(raw: RawEvent): SSEEventData | null {
  const type = (raw.type ?? raw.event) as SSEEventType | undefined;
  const data = raw.data;
  if (!type || data === undefined) return null;
  return { type, data } as SSEEventData;
}

/**
 * 将后端事件列表转为前端 SSEEventData[]
 */
export function normalizeEvents(rawList: unknown): SSEEventData[] {
  if (!Array.isArray(rawList)) return [];
  const out: SSEEventData[] = [];
  for (const raw of rawList) {
    const normalized = normalizeEvent(raw as RawEvent);
    if (normalized) out.push(normalized);
  }
  return out;
}

/** 时间线单项：runId 用于标识该展示项所属的 Agent 运行。 */
type TimelineItemBase = {
  id: string;
  runId?: string;
};

export type TimelineItem =
  | (TimelineItemBase & { kind: "user"; data: ChatMessage })
  | (TimelineItemBase & {
      kind: "attachments";
      role: "user" | "assistant";
      files: AttachmentFile[];
    })
  | (TimelineItemBase & { kind: "assistant"; data: ChatMessage })
  | (TimelineItemBase & { kind: "tool"; data: ToolEvent; timeLabel?: string })
  | (TimelineItemBase & { kind: "step"; data: StepEvent; tools: ToolEvent[] })
  | (TimelineItemBase & { kind: "error"; error: string; timestamp?: number });

/** 附件展示用（文件名、类型、大小等） */
export type AttachmentFile = {
  id: string;
  filename: string;
  extension: string;
  size: number;
  sizeLabel?: string;
};

/** 从 SessionFile 转为 AttachmentFile */
export function sessionFileToAttachment(f: SessionFile): AttachmentFile {
  return {
    id: f.id,
    filename: f.filename,
    extension: f.extension,
    size: f.size,
  };
}

/** 从 ChatMessage.attachments 项转为 AttachmentFile（无 size 时用 0） */
export function chatAttachmentToDisplay(a: {
  file_id?: string;
  id?: string;
  filename: string;
  size?: number;
  [key: string]: unknown;
}): AttachmentFile {
  const ext = (a.filename || "").split(".").pop() || "";
  return {
    id: a.file_id || a.id || "",
    filename: a.filename || "",
    extension: ext,
    size: typeof a.size === "number" ? a.size : 0,
  };
}

function getEventId(event: SSEEventData): string | undefined {
  return event?.data?.event_id;
}

function getRunId(event: SSEEventData): string | undefined {
  return event.data.run_id;
}

function timelineId(
  prefix: string,
  event: SSEEventData,
  fallback: string,
): string {
  return `${prefix}:${getEventId(event) ?? fallback}`;
}

/**
 * 按 event_id 合并历史事件与实时事件。
 *
 * 已存在的事件保持原位置，后到的数据覆盖旧数据。没有 event_id 的兼容事件
 * 无法可靠去重，因此按原顺序保留。
 */
export function mergeSessionEvents(
  ...eventGroups: ReadonlyArray<ReadonlyArray<SSEEventData>>
): SSEEventData[] {
  const merged: SSEEventData[] = [];
  const eventIndexById = new Map<string, number>();

  for (const events of eventGroups) {
    for (const event of events) {
      const eventId = getEventId(event);
      const existingIndex = eventId ? eventIndexById.get(eventId) : undefined;

      if (existingIndex !== undefined) {
        merged[existingIndex] = event;
        continue;
      }

      if (eventId) eventIndexById.set(eventId, merged.length);
      merged.push(event);
    }
  }
  return merged;
}

/** 将时间戳格式化为相对时间，如 2天前、刚刚 */
function formatTimeLabel(ts: number | string | undefined): string | undefined {
  if (ts === undefined || ts === null) return undefined;
  let t = typeof ts === "string" ? parseInt(ts, 10) : ts;
  if (Number.isNaN(t)) return undefined;

  // 后端返回的是秒级时间戳（10位数），需要转为毫秒级（13位数）
  if (t < 10000000000) {
    t = t * 1000;
  }

  const now = Date.now();
  const diff = now - t;
  if (diff < 0) return "刚刚";
  if (diff < 60 * 1000) return "刚刚";
  if (diff < 60 * 60 * 1000) return `${Math.floor(diff / (60 * 1000))}分钟前`;
  if (diff < 24 * 60 * 60 * 1000)
    return `${Math.floor(diff / (60 * 60 * 1000))}小时前`;
  if (diff < 2 * 24 * 60 * 60 * 1000) return "昨天";
  if (diff < 7 * 24 * 60 * 60 * 1000)
    return `${Math.floor(diff / (24 * 60 * 60 * 1000))}天前`;
  if (diff < 30 * 24 * 60 * 60 * 1000)
    return `${Math.floor(diff / (7 * 24 * 60 * 60 * 1000))}周前`;
  return undefined;
}

export function getToolTimeLabel(tool: ToolEvent): string | undefined {
  const ts =
    typeof tool.timestamp === "number"
      ? tool.timestamp
      : (tool.created_at ??
        (typeof tool.ts === "number" ? tool.ts : undefined));
  return formatTimeLabel(ts);
}

type ToolLocation =
  | { kind: "standalone"; timelineIndex: number }
  | { kind: "step"; timelineIndex: number; toolIndex: number };

type RunContext = {
  activeStepIndex: number | null;
  toolLocationByCallId: Map<string, ToolLocation>;
};

function createRunContext(): RunContext {
  return {
    activeStepIndex: null,
    toolLocationByCallId: new Map<string, ToolLocation>(),
  };
}

function updateToolAtLocation(
  timeline: TimelineItem[],
  location: ToolLocation,
  tool: ToolEvent,
): boolean {
  const item = timeline[location.timelineIndex];

  if (location.kind === "standalone" && item?.kind === "tool") {
    timeline[location.timelineIndex] = {
      ...item,
      data: tool,
      timeLabel: getToolTimeLabel(tool),
    };
    return true;
  }

  if (location.kind === "step" && item?.kind === "step") {
    item.tools[location.toolIndex] = tool;
    return true;
  }

  return false;
}

/**
 * 将 SSE 事件列表归并为时间线展示项（顺序与设计一致）
 */
export function eventsToTimeline(events: SSEEventData[]): TimelineItem[] {
  const timeline: TimelineItem[] = [];
  const contextByRunId = new Map<string, RunContext>();
  const legacyContext = createRunContext();

  for (const [eventIndex, ev] of mergeSessionEvents(events).entries()) {
    const runId = getRunId(ev);
    let context = legacyContext; // 默认上下文（无 runId 的事件使用同一个上下文）
    if (runId) {
      const existingContext = contextByRunId.get(runId);
      if (existingContext) {
        context = existingContext;
      } else {
        context = createRunContext();
        contextByRunId.set(runId, context);
      }
    }

    switch (ev.type) {
      case "message": {
        const msg: ChatMessage = ev.data;
        const messageId = timelineId(
          msg.role,
          ev,
          `${msg.role}:${msg.created_at ?? eventIndex}`,
        );

        // 这2种类型的需要添加
        if (["user", "assistant"].includes(msg.role)) {
          timeline.push({
            kind: msg.role as "user" | "assistant",
            id: messageId,
            runId,
            data: msg,
          });

          if (msg.attachments && msg.attachments.length > 0) {
            timeline.push({
              kind: "attachments",
              id: `${messageId}:attachments`,
              runId,
              role: msg.role as "user" | "assistant",
              files: msg.attachments.map(chatAttachmentToDisplay),
            });
          }
        }
        break;
      }
      case "step": {
        const step: StepEvent = ev.data;
        const currentStepIndex = context.activeStepIndex;
        const activeStep =
          currentStepIndex === null ? undefined : timeline[currentStepIndex];

        if (
          currentStepIndex !== null &&
          activeStep?.kind === "step" &&
          activeStep.data.id === step.id
        ) {
          timeline[currentStepIndex] = { ...activeStep, data: step };
        } else {
          // 第一次在这里
          context.activeStepIndex = timeline.length; // 设置当前激活的step
          timeline.push({
            kind: "step",
            id: timelineId("step", ev, step.id),
            runId,
            data: step,
            tools: [],
          });
        }
        // 判断当前步骤是否完结
        if (step.status === "completed" || step.status === "failed") {
          context.activeStepIndex = null;
        }
        break;
      }
      case "tool": {
        const tool: ToolEvent = ev.data;
        const toolCallId = tool.tool_call_id; // 找到tool_call_id
        const existingLocation = toolCallId
          ? context.toolLocationByCallId.get(toolCallId)
          : undefined;

        // 如果已经存在该 toolCallId 的位置，则更新该位置的 tool 数据，覆盖状态数据等
        if (
          existingLocation &&
          updateToolAtLocation(timeline, existingLocation, tool)
        ) {
          break;
        }

        const currentStepIndex = context.activeStepIndex;
        const activeStep =
          currentStepIndex === null ? undefined : timeline[currentStepIndex];

        if (currentStepIndex !== null && activeStep?.kind === "step") {
          const toolIndex = activeStep.tools.length;
          activeStep.tools.push(tool);
          if (toolCallId) {
            context.toolLocationByCallId.set(toolCallId, {
              kind: "step",
              timelineIndex: currentStepIndex,
              toolIndex,
            });
          }
        } else {
          const timelineIndex = timeline.length;
          timeline.push({
            kind: "tool",
            id: `tool:${runId ?? "legacy"}:${toolCallId ?? getEventId(ev) ?? eventIndex}`,
            runId,
            data: tool,
            timeLabel: getToolTimeLabel(tool),
          });
          if (toolCallId) {
            context.toolLocationByCallId.set(toolCallId, {
              kind: "standalone",
              timelineIndex,
            });
          }
        }
        break;
      }
      case "title":
      case "plan":
        break;
      case "wait":
      case "done": {
        context.activeStepIndex = null;
        break;
      }
      case "error": {
        context.activeStepIndex = null;
        const errorData = ev.data;
        if (errorData.error) {
          timeline.push({
            kind: "error",
            id: timelineId(
              "error",
              ev,
              `${errorData.created_at ?? eventIndex}`,
            ),
            runId,
            error: errorData.error,
            timestamp:
              typeof errorData.created_at === "number"
                ? errorData.created_at
                : undefined,
          });
        }
        break;
      }
      default:
        break;
    }
  }

  return timeline;
}

/**
 * 从事件列表中取最新的 plan 步骤（用于底部任务进度面板）
 */
export function getLatestPlanFromEvents(events: SSEEventData[]): PlanStep[] {
  let steps: PlanStep[] = [];
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i];
    if (ev.type === "plan") {
      const plan = ev.data as PlanEvent;
      if (plan.steps && Array.isArray(plan.steps)) {
        steps = plan.steps;
      }
      break;
    }
  }
  return steps;
}
