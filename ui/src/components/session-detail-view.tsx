"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { SessionHeader } from "@/components/session-header";
import { ChatInput } from "@/components/chat-input";
import { PlanPanel } from "@/components/plan-panel";
import {
  SessionTimeline,
  type SessionTimelineHandle,
} from "@/components/session-timeline";
import { FilePreviewPanel } from "@/components/file-preview-panel";
import { ToolPreviewPanel } from "@/components/tool-preview-panel";
import { VNCOverlay } from "@/components/vnc-overlay";
import { useSessionDetail } from "@/hooks/use-session-detail";
import { getToolKind } from "@/components/tool-use/utils";
import {
  eventsToTimeline,
  getLatestPlanFromEvents,
} from "@/lib/session-events";
import type { ToolEvent, FileInfo } from "@/lib/api/types";
import type { AttachmentFile, TimelineItem } from "@/lib/session-events";
import { sessionApi } from "@/lib/api/session";
import { toast } from "sonner";
import { Loader2 } from "lucide-react";

export interface SessionDetailViewProps {
  sessionId: string;
  initialMessage?: string;
  initialAttachments?: string[];
  hasInitialMessage?: boolean;
}

type TimelineToolIndex = {
  byIdentity: Map<string, ToolEvent>;
  latestPreviewable: ToolEvent | null;
};

function getToolIdentity(
  tool: ToolEvent,
  fallbackRunId?: string,
): string | null {
  if (!tool.tool_call_id) return null;
  return JSON.stringify([
    tool.run_id ?? fallbackRunId ?? null,
    tool.tool_call_id,
  ]);
}

/** 单次遍历建立工具索引，供预览解析、自动追踪和跳转复用。 */
function buildTimelineToolIndex(timeline: TimelineItem[]): TimelineToolIndex {
  const byIdentity = new Map<string, ToolEvent>();
  let latestPreviewable: ToolEvent | null = null;

  for (const item of timeline) {
    const tools =
      item.kind === "tool"
        ? [item.data]
        : item.kind === "step"
          ? item.tools
          : [];

    for (const tool of tools) {
      const identity = getToolIdentity(tool, item.runId);
      if (identity) byIdentity.set(identity, tool);
      if (getToolKind(tool) !== "message") latestPreviewable = tool;
    }
  }

  return { byIdentity, latestPreviewable };
}

export function SessionDetailView({
  sessionId,
  initialMessage,
  initialAttachments,
  hasInitialMessage,
}: SessionDetailViewProps) {
  const [fileListOpen, setFileListOpen] = useState(false);
  const [previewFile, setPreviewFile] = useState<AttachmentFile | null>(null);
  const [previewTool, setPreviewTool] = useState<ToolEvent | null>(null);
  const [vncOpen, setVncOpen] = useState(false);
  const [replyToWaiting, setReplyToWaiting] = useState(false);
  const [initialMessageStarted, setInitialMessageStarted] =
    useState(!hasInitialMessage);
  const initialMessageSentRef = useRef(false);
  const timelineRef = useRef<SessionTimelineHandle>(null);
  const vncOpenRef = useRef(false);

  const handleRealtimeTool = useCallback((tool: ToolEvent) => {
    if (vncOpenRef.current) return;
    if (getToolKind(tool) === "message") return;
    setPreviewTool(tool);
    setPreviewFile(null);
    timelineRef.current?.scrollToLatest("smooth");
  }, []);

  const {
    session,
    files,
    sessionSSEEvent,
    loading,
    error,
    hasMoreEvents,
    loadingMoreEvents,
    loadMoreError,
    loadMoreEvents,
    refresh,
    refreshFiles,
    sendMessage,
    streaming,
  } = useSessionDetail(sessionId, hasInitialMessage, handleRealtimeTool);

  const timeline = useMemo(
    () => eventsToTimeline(sessionSSEEvent),
    [sessionSSEEvent],
  );
  const planSteps = useMemo(
    () => getLatestPlanFromEvents(sessionSSEEvent),
    [sessionSSEEvent],
  );
  const timelineToolIndex = useMemo(
    () => buildTimelineToolIndex(timeline),
    [timeline],
  );

  const hasPreview = previewFile !== null || previewTool !== null;

  /**
   * 将 previewTool 解析为 timeline 中最新版本的工具对象。
   * 自动跟踪设置 previewTool 时工具事件可能尚无 content（如截图），
   * 后续 SSE 更新后 timeline 中对象已刷新但 state 仍为旧引用。
   * 通过 run_id + tool_call_id 从索引中获取最新版本。
   */
  const resolvedPreviewTool = useMemo(() => {
    if (!previewTool) return null;
    const identity = getToolIdentity(previewTool);
    return identity
      ? (timelineToolIndex.byIdentity.get(identity) ?? previewTool)
      : previewTool;
  }, [previewTool, timelineToolIndex]);

  useEffect(() => {
    if (
      initialMessage &&
      !initialMessageSentRef.current &&
      session &&
      !loading &&
      !streaming
    ) {
      initialMessageSentRef.current = true;
      setInitialMessageStarted(true);
      sendMessage(initialMessage, initialAttachments || [])
        .then(() => {
          setTimeout(() => {
            window.history.replaceState(null, "", `/sessions/${sessionId}`);
          }, 100);
        })
        .catch((e) => {
          toast.error(e instanceof Error ? e.message : "发送消息失败");
        });
    }
  }, [
    initialMessage,
    initialAttachments,
    session,
    loading,
    streaming,
    sendMessage,
    sessionId,
  ]);

  const handleSend = useCallback(
    async (message: string, uploadedFiles: FileInfo[]) => {
      try {
        const attachmentIds = uploadedFiles.map((f) => f.id);
        const resumeRunId = replyToWaiting && session?.status === "waiting"
          ? session.run_id : null;
        await sendMessage(message, attachmentIds, resumeRunId ?? undefined);
        setReplyToWaiting(false);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : "发送失败，请重试");
        throw e;
      }
    },
    [sendMessage, session?.status, session?.run_id, replyToWaiting],
  );

  const handleResume = useCallback(async () => {
    if (!session?.run_id || !session.can_resume ||
        !["interrupted", "failed"].includes(session.status)) return;
    try {
      await sendMessage("继续执行这个任务", [], session.run_id);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "恢复任务失败");
    }
  }, [sendMessage, session]);

  const handleViewAllFiles = useCallback(() => {
    refreshFiles();
    setFileListOpen(true);
  }, [refreshFiles]);

  const handleFileClick = useCallback((file: AttachmentFile) => {
    setPreviewFile(file);
    setPreviewTool(null);
  }, []);

  const handleToolClick = useCallback((tool: ToolEvent) => {
    const kind = getToolKind(tool);
    if (kind === "message") return;
    setPreviewTool(tool);
    setPreviewFile(null);
  }, []);

  const handleClosePreview = useCallback(() => {
    setPreviewFile(null);
    setPreviewTool(null);
  }, []);

  const handleJumpToLatest = useCallback(() => {
    const latest = timelineToolIndex.latestPreviewable;
    if (latest) {
      setPreviewTool(latest);
      setPreviewFile(null);
    }
    timelineRef.current?.scrollToLatest("smooth");
  }, [timelineToolIndex]);

  const handleOpenVNC = useCallback(() => {
    vncOpenRef.current = true;
    setVncOpen(true);
  }, []);

  const handleCloseVNC = useCallback(() => {
    vncOpenRef.current = false;
    setVncOpen(false);
    // 关闭 VNC 后跳转到最新工具
    const latest = timelineToolIndex.latestPreviewable;
    if (latest && session?.status === "running") {
      setPreviewTool(latest);
      setPreviewFile(null);
      setTimeout(() => {
        timelineRef.current?.scrollToLatest("smooth");
      }, 100);
    }
  }, [timelineToolIndex, session?.status]);

  const handleStop = useCallback(async () => {
    if (!session) return;
    try {
      await sessionApi.stopSession(sessionId);
      toast.success("任务已停止");
      refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "停止任务失败");
    }
  }, [session, sessionId, refresh]);

  if (loading && !session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center">
        {hasInitialMessage ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" />
            <span>正在思考中...</span>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">加载中...</p>
        )}
      </div>
    );
  }

  if (error && !session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center gap-2">
        <p className="text-sm text-red-600">{error.message}</p>
        <button
          type="button"
          onClick={() => refresh()}
          className="text-sm text-primary underline"
        >
          重试
        </button>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="relative flex flex-col h-full flex-1 min-w-0 px-4 items-center justify-center">
        <p className="text-sm text-muted-foreground">未找到该任务</p>
      </div>
    );
  }

  return (
    <>
      <div className="flex flex-row h-screen w-full overflow-hidden">
        {/* 主内容区 */}
        <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
          <div
            className={`flex flex-col h-full mx-auto w-full min-w-0 px-4 ${hasPreview ? "" : "max-w-[768px]"}`}
          >
            <div className="shrink-0">
              <SessionHeader
                title={session.title}
                files={files}
                fileListOpen={fileListOpen}
                onFileListOpenChange={setFileListOpen}
                onFetchFiles={refreshFiles}
                onFileClick={handleFileClick}
              />
            </div>

            <SessionTimeline
              key={sessionId}
              ref={timelineRef}
              timeline={timeline}
              hasMoreEvents={hasMoreEvents}
              loadingMoreEvents={loadingMoreEvents}
              loadMoreError={loadMoreError}
              empty={
                timeline.length === 0 && !streaming && !hasInitialMessage
              }
              thinking={session.status === "running" || !initialMessageStarted}
              onLoadMore={loadMoreEvents}
              onViewAllFiles={handleViewAllFiles}
              onFileClick={handleFileClick}
              onToolClick={handleToolClick}
            />

            <div className="shrink-0 bg-background/80 backdrop-blur-sm py-4 relative">
              <div className="absolute top-0 left-1/2 -translate-x-1/2 w-[200px] h-px bg-linear-to-r from-transparent via-[#7c5cf0]/30 to-transparent" />
              <PlanPanel className="mb-2" steps={planSteps} />
              {session.status === "waiting" && session.can_resume && (
                <button
                  type="button"
                  onClick={() => setReplyToWaiting((value) => !value)}
                  disabled={streaming}
                  className="mb-2 rounded-md border px-3 py-1 text-sm disabled:opacity-50"
                >
                  {replyToWaiting
                    ? "正在回复上个任务（点击改为新任务）"
                    : "回复上个任务（默认发送新任务）"}
                </button>
              )}
              {session.can_resume &&
                ["interrupted", "failed"].includes(session.status) && (
                  <button
                    type="button"
                    onClick={handleResume}
                    disabled={streaming}
                    className="mb-2 rounded-md border px-3 py-1 text-sm disabled:opacity-50"
                  >
                    继续上一个任务
                  </button>
                )}
              <ChatInput
                onSend={handleSend}
                sessionId={sessionId}
                isRunning={session?.status === "running"}
                onStop={handleStop}
              />
            </div>
          </div>
        </div>

        {/* 文件预览面板 */}
        {previewFile && (
          <div className="shrink-0 w-[600px] h-full animate-in slide-in-from-right duration-300">
            <FilePreviewPanel file={previewFile} onClose={handleClosePreview} />
          </div>
        )}

        {/* 工具预览面板 */}
        {resolvedPreviewTool && (
          <div className="shrink-0 w-[600px] h-full py-2 pr-2 animate-in slide-in-from-right duration-300">
            <ToolPreviewPanel
              tool={resolvedPreviewTool}
              onClose={handleClosePreview}
              onJumpToLatest={handleJumpToLatest}
              onOpenVNC={
                getToolKind(resolvedPreviewTool) === "browser"
                  ? handleOpenVNC
                  : undefined
              }
            />
          </div>
        )}
      </div>

      {/* noVNC 全屏远程桌面覆盖层 */}
      {vncOpen && <VNCOverlay sessionId={sessionId} onClose={handleCloseVNC} />}
    </>
  );
}
