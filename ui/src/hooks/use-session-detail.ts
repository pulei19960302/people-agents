"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { sessionApi } from "@/lib/api/session";
import { normalizeEvent, normalizeEvents } from "@/lib/session-events";
import type { SessionDetail, SSEEventData, SessionFile } from "@/lib/api/types";

const EMPTY_STREAM_RETRY_DELAYS = [1000, 2000, 4000] as const;

export type UseSessionDetailResult = {
  session: SessionDetail | null;
  files: SessionFile[];
  events: SSEEventData[];
  loading: boolean;
  error: Error | null;
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
): UseSessionDetailResult {
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [files, setFiles] = useState<SessionFile[]>([]);
  const [events, setEvents] = useState<SSEEventData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
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
  const sessionStatusRef = useRef<SessionDetail["status"] | null>(null);

  const updateSessionStatus = useCallback((status: SessionDetail["status"]) => {
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

      setEvents((prev) => [...prev, evToAppend]);

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
        updateSessionStatus("completed");
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
      const [detail, fileListRaw] = await Promise.all([
        sessionApi.getSessionDetail(sessionId),
        sessionApi.getSessionFiles(sessionId),
      ]);
      sessionStatusRef.current = detail.status;
      setSession(detail);
      setFiles(normalizeFileList(fileListRaw));
      const rawEvents = (detail as { events?: unknown }).events;
      if (rawEvents && Array.isArray(rawEvents) && rawEvents.length > 0) {
        const normalized = normalizeEvents(rawEvents);
        setEvents(normalized);
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
    if (!sessionId) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoading(false);
      setSession(null);
      setFiles([]);
      setEvents([]);
      setError(null);
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
          updateSessionStatus("completed");
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
    files,
    events,
    loading,
    error,
    refresh,
    refreshFiles,
    sendMessage,
    streaming,
  };
}
