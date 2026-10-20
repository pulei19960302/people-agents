"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { sessionApi } from "@/lib/api/session";
import {
  mergeSessionEvents,
  normalizeEvent,
  normalizeEvents,
} from "@/lib/session-events";
import type { Session, SSEEventData, SessionFile, ToolEvent } from "@/lib/api/types";

const EMPTY_STREAM_RETRY_DELAYS = [1000, 2000, 4000] as const;
const TIMELINE_RUN_LIMIT = 1;
const MIN_LOAD_MORE_DURATION_MS = 500;

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export type UseSessionDetailResult = {
  session: Session | null;
  sessionSSEEvent: SSEEventData[];
  files: SessionFile[];
  loading: boolean;
  error: Error | null;
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  loadMoreError: Error | null;
  loadMoreEvents: () => Promise<boolean>;
  refresh: () => Promise<void>;
  refreshFiles: () => Promise<void>;
  sendMessage: (message: string, attachmentIds: string[]) => Promise<void>;
  streaming: boolean;
};

/**
 * 任务详情：拉取会话详情与文件列表，管理事件列表；
 * 运行中任务会通过 chat 空 body 流式拉取事件，发送消息时通过 chat 带 body 流式追加事件。
 */
export function useSessionDetail(
  sessionId: string | null,
  initialSkipEmptyStream?: boolean,
  onRealtimeTool?: (tool: ToolEvent) => void,
): UseSessionDetailResult {
  const [session, setSession] = useState<Session | null>(null);
  const [sessionSSEEvent, setSessionSSEEvent] = useState<SSEEventData[]>([]);
  const [files, setFiles] = useState<SessionFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const [nextEventCursor, setNextEventCursor] = useState<string | null>(null);
  const [loadingMoreEvents, setLoadingMoreEvents] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState<Error | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [skipEmptyStream, setSkipEmptyStream] = useState(
    initialSkipEmptyStream || false,
  );
  const emptyStreamCleanupRef = useRef<(() => void) | null>(null);
  const emptyStreamRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(
    null,
  );
  const emptyStreamRetryCountRef = useRef(0);
  const startEmptyStreamRef = useRef<() => void>(() => undefined);
  const messageStreamCleanupRef = useRef<(() => void) | null>(null);
  const isSendMessageRef = useRef(false);
  const lastEventIdRef = useRef<string | null>(null);
  const sessionStatusRef = useRef<Session["status"] | null>(null);
  const loadingMoreEventsRef = useRef(false);
  const activeSessionIdRef = useRef(sessionId);
  const loadedSessionIdRef = useRef<string | null>(null);
  const onRealtimeToolRef = useRef(onRealtimeTool);

  useEffect(() => {
    activeSessionIdRef.current = sessionId;
  }, [sessionId]);

  useEffect(() => {
    onRealtimeToolRef.current = onRealtimeTool;
  }, [onRealtimeTool]);

  const updateSessionStatus = useCallback((status: Session["status"]) => {
    sessionStatusRef.current = status;
    setSession((prev) => (prev ? { ...prev, status } : null));
  }, []);

  const appendEvent = useCallback(
    (ev: SSEEventData) => {
      let evToAppend = ev;
      if (
        ev.data &&
        typeof ev.data === "object" &&
        ("event" in ev.data || "type" in ev.data) &&
        "data" in ev.data
      ) {
        const normalized = normalizeEvent(
          ev.data as { event?: string; type?: string; data?: unknown },
        );
        if (normalized) evToAppend = normalized;
      }

      const eventId = (evToAppend.data as { event_id?: string })?.event_id;
      if (eventId) lastEventIdRef.current = eventId;

      setSessionSSEEvent((prev) => mergeSessionEvents(prev, [evToAppend]));

      if (evToAppend.type === "tool") {
        onRealtimeToolRef.current?.(evToAppend.data);
      }

      // 更新会话标题
      if (
        evToAppend.type === "title" &&
        evToAppend.data &&
        typeof (evToAppend.data as { title?: string }).title === "string"
      ) {
        setSession((prev) =>
          prev
            ? { ...prev, title: (evToAppend.data as { title: string }).title }
            : null,
        );
      }

      // 监听事件更新会话状态
      if (evToAppend.type === "step") {
        const stepData = evToAppend.data as { status?: string };
        if (stepData.status === "running") {
          updateSessionStatus("running");
        }
        if (stepData.status === "waiting") {
          updateSessionStatus("waiting");
          setStreaming(false);
        }
      }

      // message_ask_user calling → 等待用户输入，切换为 waiting
      if (evToAppend.type === "tool") {
        const toolData = evToAppend.data as {
          function?: string;
          status?: string;
        };
        if (
          toolData.function === "message_ask_user" &&
          toolData.status === "calling"
        ) {
          updateSessionStatus("waiting");
          setStreaming(false);
        }
      }

      // wait 事件 → 等待用户输入
      if (evToAppend.type === "wait") {
        updateSessionStatus("waiting");
        setStreaming(false);
      }

      // done 事件时更新为 completed
      if (evToAppend.type === "done") {
        updateSessionStatus("completed");
      }

      // error 事件时也可以认为任务结束
      if (evToAppend.type === "error") {
        updateSessionStatus("failed");
        setStreaming(false);
      }
    },
    [updateSessionStatus],
  );

  const startEmptyStream = useCallback(() => {
    if (
      !sessionId ||
      sessionStatusRef.current !== "running" ||
      isSendMessageRef.current
    )
      return;
    if (emptyStreamRetryTimerRef.current) {
      clearTimeout(emptyStreamRetryTimerRef.current);
      emptyStreamRetryTimerRef.current = null;
    }
    if (emptyStreamCleanupRef.current) {
      emptyStreamCleanupRef.current();
      emptyStreamCleanupRef.current = null;
    }
    emptyStreamCleanupRef.current = sessionApi.chat(
      sessionId,
      { event_id: lastEventIdRef.current || undefined },
      (ev) => {
        emptyStreamRetryCountRef.current = 0;
        appendEvent(ev);
      },
      (err) => {
        if (err.name === "AbortError") {
          return;
        }
        emptyStreamCleanupRef.current = null;

        // 终止事件会先更新 status；waiting/completed 时不应继续订阅。
        if (
          sessionStatusRef.current !== "running" ||
          isSendMessageRef.current
        ) {
          return;
        }

        const retryIndex = emptyStreamRetryCountRef.current;
        if (retryIndex >= EMPTY_STREAM_RETRY_DELAYS.length) {
          console.warn("Session detail stream stopped after retry limit:", err);
          return;
        }

        if (err.message !== "SSE_STREAM_END") {
          console.warn("Session detail empty stream error:", err);
        }
        emptyStreamRetryCountRef.current += 1;
        emptyStreamRetryTimerRef.current = setTimeout(() => {
          emptyStreamRetryTimerRef.current = null;
          if (
            sessionStatusRef.current === "running" &&
            !isSendMessageRef.current
          ) {
            startEmptyStreamRef.current();
          }
        }, EMPTY_STREAM_RETRY_DELAYS[retryIndex]);
      },
    );
  }, [sessionId, appendEvent]);

  useEffect(() => {
    startEmptyStreamRef.current = startEmptyStream;
  }, [startEmptyStream]);

  const stopEmptyStream = useCallback(() => {
    if (emptyStreamRetryTimerRef.current) {
      clearTimeout(emptyStreamRetryTimerRef.current);
      emptyStreamRetryTimerRef.current = null;
    }
    if (emptyStreamCleanupRef.current) {
      emptyStreamCleanupRef.current();
      emptyStreamCleanupRef.current = null;
    }
    emptyStreamRetryCountRef.current = 0;
  }, []);

  const normalizeFileList = useCallback((raw: unknown): SessionFile[] => {
    if (Array.isArray(raw)) return raw as SessionFile[];
    if (
      raw &&
      typeof raw === "object" &&
      "files" in raw &&
      Array.isArray((raw as { files: unknown }).files)
    ) {
      return (raw as { files: SessionFile[] }).files;
    }
    if (
      raw &&
      typeof raw === "object" &&
      "data" in raw &&
      Array.isArray((raw as { data: unknown }).data)
    ) {
      return (raw as { data: SessionFile[] }).data;
    }
    return [];
  }, []);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    setError(null);
    try {
      const [detail, fileListRaw, sseEvents] = await Promise.all([
        sessionApi.getSession(sessionId),
        sessionApi.getSessionFiles(sessionId),
        sessionApi.getSessionEventTimeline(sessionId, {
          limit: TIMELINE_RUN_LIMIT,
        }),
      ]);

      if (activeSessionIdRef.current !== sessionId) return;

      sessionStatusRef.current = detail.status;
      setSession(detail);
      setFiles(normalizeFileList(fileListRaw));
      const rawEvents = sseEvents?.events || [];
      const normalized = normalizeEvents(rawEvents);
      const isInitialLoad = loadedSessionIdRef.current !== sessionId;
      if (isInitialLoad) {
        setSessionSSEEvent(normalized);
        setNextEventCursor(sseEvents.next_cursor);
        setHasMoreEvents(Boolean(sseEvents.has_more && sseEvents.next_cursor));
        loadedSessionIdRef.current = sessionId;
      } else {
        // 刷新只同步最新一页，不丢弃用户已经上拉加载的历史页和分页游标。
        setSessionSSEEvent((currentEvents) =>
          mergeSessionEvents(currentEvents, normalized),
        );
      }
      setLoadMoreError(null);

      if (normalized.length > 0) {
        const lastEvId = (
          normalized[normalized.length - 1]?.data as { event_id?: string }
        )?.event_id;
        if (lastEvId) lastEventIdRef.current = lastEvId;
      }
    } catch (e) {
      setError(e instanceof Error ? e : new Error("加载失败"));
    } finally {
      setLoading(false);
    }
  }, [sessionId, normalizeFileList]);

  const loadMoreEvents = useCallback(async (): Promise<boolean> => {
    if (
      !sessionId ||
      !hasMoreEvents ||
      !nextEventCursor ||
      loadingMoreEventsRef.current
    ) {
      return false;
    }

    const requestedSessionId = sessionId;
    const requestedCursor = nextEventCursor;
    loadingMoreEventsRef.current = true;
    setLoadingMoreEvents(true);
    setLoadMoreError(null);

    try {
      const startedAt = Date.now();
      const page = await sessionApi.getSessionEventTimeline(
        requestedSessionId,
        {
          limit: TIMELINE_RUN_LIMIT,
          before: requestedCursor,
        },
      );

      const remainingDelay =
        MIN_LOAD_MORE_DURATION_MS - (Date.now() - startedAt);
      if (remainingDelay > 0) {
        await delay(remainingDelay);
      }

      if (activeSessionIdRef.current !== requestedSessionId) return false;

      if (page.has_more && page.next_cursor === requestedCursor) {
        setLoadMoreError(new Error("分页游标未推进，请重新加载后重试"));
        return false;
      }

      const olderEvents = normalizeEvents(page.events);
      setSessionSSEEvent((currentEvents) =>
        mergeSessionEvents(olderEvents, currentEvents),
      );
      setNextEventCursor(page.next_cursor);
      setHasMoreEvents(Boolean(page.has_more && page.next_cursor));
      return true;
    } catch (loadError) {
      if (activeSessionIdRef.current === requestedSessionId) {
        setLoadMoreError(
          loadError instanceof Error
            ? loadError
            : new Error("加载更早记录失败"),
        );
      }
      return false;
    } finally {
      loadingMoreEventsRef.current = false;
      if (activeSessionIdRef.current === requestedSessionId) {
        setLoadingMoreEvents(false);
      }
    }
  }, [hasMoreEvents, nextEventCursor, sessionId]);

  const refreshFiles = useCallback(async () => {
    if (!sessionId) return;
    try {
      const fileListRaw = await sessionApi.getSessionFiles(sessionId);
      setFiles(normalizeFileList(fileListRaw));
    } catch (e) {
      console.error("刷新文件列表失败:", e);
    }
  }, [sessionId, normalizeFileList]);

  useEffect(() => {
    loadedSessionIdRef.current = null;

    if (!sessionId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoading(false);
      setSession(null);
      setFiles([]);
      setSessionSSEEvent([]);
      setError(null);
      setHasMoreEvents(false);
      setNextEventCursor(null);
      setLoadingMoreEvents(false);
      setLoadMoreError(null);
      loadingMoreEventsRef.current = false;
      sessionStatusRef.current = null;
      stopEmptyStream();
      return;
    }
    setLoading(true);
    refresh().then(() => {
      // 由下面的 effect 根据 session 状态决定是否开空流
    });
    return () => {
      stopEmptyStream();
    };
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const sessionStatus = session?.status;

  useEffect(() => {
    if (!sessionId || !sessionStatus) return;
    // 如果标记了跳过空流（比如有初始消息待发送），则不启动空流
    if (
      sessionStatus === "running" &&
      !isSendMessageRef.current &&
      !skipEmptyStream
    ) {
      emptyStreamRetryCountRef.current = 0;
      startEmptyStream();
    }
    return () => {
      stopEmptyStream();
    };
  }, [
    sessionId,
    sessionStatus,
    skipEmptyStream,
    startEmptyStream,
    stopEmptyStream,
  ]);

  // 组件卸载时清理消息流
  useEffect(() => {
    return () => {
      if (messageStreamCleanupRef.current) {
        messageStreamCleanupRef.current();
        messageStreamCleanupRef.current = null;
      }
    };
  }, []);

  const sendMessage = useCallback(
    async (message: string, attachmentIds: string[]) => {
      if (!sessionId) return;
      stopEmptyStream();
      // 清理已有的消息流连接（如 waiting 状态时用户再次发送）
      if (messageStreamCleanupRef.current) {
        messageStreamCleanupRef.current();
        messageStreamCleanupRef.current = null;
      }
      // 发送消息时，清除跳过空流的标记
      setSkipEmptyStream(false);
      isSendMessageRef.current = true;
      setStreaming(true);

      // 立即更新状态为 running，不等待 SSE 事件
      updateSessionStatus("running");

      const onEvent = (ev: SSEEventData) => {
        appendEvent(ev);
        if (ev.type === "done" || ev.type === "error" || ev.type === "wait") {
          setStreaming(false);
          isSendMessageRef.current = false;
          if (messageStreamCleanupRef.current) {
            messageStreamCleanupRef.current();
            messageStreamCleanupRef.current = null;
          }
        }
      };
      const messageStreamCleanup = sessionApi.chat(
        sessionId,
        { message, attachments: attachmentIds },
        onEvent,
        (err) => {
          if (err.name === "AbortError") {
            setStreaming(false);
            isSendMessageRef.current = false;
            return;
          }
          // 未收到终止事件却结束时，先以服务端状态为准，再尝试受限重连。
          if (err.message === "SSE_STREAM_END") {
            setStreaming(false);
            isSendMessageRef.current = false;
            if (messageStreamCleanupRef.current) {
              messageStreamCleanupRef.current();
              messageStreamCleanupRef.current = null;
            }
            void refresh().then(() => startEmptyStream());
            return;
          }
          // 实际错误
          setError(err instanceof Error ? err : new Error("流式响应异常"));
          setStreaming(false);
          isSendMessageRef.current = false;
          void refresh()
          if (messageStreamCleanupRef.current) {
            messageStreamCleanupRef.current();
            messageStreamCleanupRef.current = null;
          }
        },
      );
      // 将消息流的 cleanup 存到独立的 ref，不与 emptyStream 混淆
      messageStreamCleanupRef.current = messageStreamCleanup;
    },
    [
      sessionId,
      appendEvent,
      refresh,
      startEmptyStream,
      stopEmptyStream,
      updateSessionStatus,
    ],
  );

  return {
    session,
    sessionSSEEvent,
    files,
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
  };
}
