"use client";

import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useRef,
  useState,
} from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import { ChevronUp, Loader2, RefreshCw } from "lucide-react";
import { ChatMessage } from "@/components/chat-message";
import { useThrottledCallback } from "@/hooks/use-throttled-callback";
import type { ToolEvent } from "@/lib/api/types";
import type { AttachmentFile, TimelineItem } from "@/lib/session-events";

const FIRST_ITEM_INDEX = 1_000_000;
const LOAD_MORE_THROTTLE_MS = 500;

export type SessionTimelineHandle = {
  scrollToLatest: (behavior?: "auto" | "smooth") => void;
};

type SessionTimelineProps = {
  timeline: TimelineItem[];
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  loadMoreError: Error | null;
  empty: boolean;
  thinking: boolean;
  onLoadMore: () => Promise<boolean>;
  onViewAllFiles: () => void;
  onFileClick: (file: AttachmentFile) => void;
  onToolClick: (tool: ToolEvent) => void;
};

type VirtualTimelineState = {
  source: TimelineItem[];
  data: TimelineItem[];
  firstItemIndex: number;
};

type TimelineContext = {
  hasMoreEvents: boolean;
  loadingMoreEvents: boolean;
  loadMoreError: Error | null;
  empty: boolean;
  hasItems: boolean;
  thinking: boolean;
  loadMore: () => void;
};

function TimelineHeader({ context }: { context: TimelineContext }) {
  const { hasMoreEvents, loadMore, loadMoreError, loadingMoreEvents } = context;

  if (!hasMoreEvents && !loadingMoreEvents && !loadMoreError) {
    return null;
  }

  return (
    <div className="flex h-12 shrink-0 items-center justify-center">
      {loadingMoreEvents ? (
        <Loader2
          className="size-4 animate-spin text-muted-foreground"
          aria-label="正在加载更早记录"
        />
      ) : loadMoreError ? (
        <button
          type="button"
          className="inline-flex size-8 items-center justify-center text-muted-foreground hover:text-foreground"
          onClick={loadMore}
          title="重新加载更早记录"
          aria-label="重新加载更早记录"
        >
          <RefreshCw className="size-4" />
        </button>
      ) : (
        <button
          type="button"
          className="inline-flex size-8 items-center justify-center text-muted-foreground hover:text-foreground"
          onClick={loadMore}
          title="加载更早记录"
          aria-label="加载更早记录"
        >
          <ChevronUp className="size-4" />
        </button>
      )}
    </div>
  );
}

function TimelineEmpty({ context }: { context: TimelineContext }) {
  if (context.thinking) {
    return (
      <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        <span>正在思考中...</span>
      </div>
    );
  }
  if (!context.empty) return null;
  return (
    <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
      暂无对话记录，在下方输入任务或提问
    </div>
  );
}

function TimelineFooter({ context }: { context: TimelineContext }) {
  return (
    <div className="pb-20">
      {context.hasItems && context.thinking && (
        <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          <span>正在思考中...</span>
        </div>
      )}
    </div>
  );
}

const timelineComponents = {
  Header: TimelineHeader,
  EmptyPlaceholder: TimelineEmpty,
  Footer: TimelineFooter,
};

/**
 * 将原始 timeline 和 Virtuoso 的逻辑索引放进同一份状态。
 * 历史项前插时同步减少 firstItemIndex，Virtuoso 会据此保持当前阅读锚点。
 */
function useVirtualTimeline(timeline: TimelineItem[]): VirtualTimelineState {
  const [state, setState] = useState<VirtualTimelineState>(() => ({
    source: timeline,
    data: timeline,
    firstItemIndex: FIRST_ITEM_INDEX,
  }));

  if (state.source !== timeline) {
    // 上一次最顶部的id
    const previousFirstId = state.data[0]?.id;

    const prependedCount = previousFirstId
      ? timeline.findIndex((item) => item.id === previousFirstId)
      : 0;
    const nextState = {
      source: timeline,
      data: timeline,
      firstItemIndex:
        prependedCount > 0
          ? state.firstItemIndex - prependedCount
          : state.firstItemIndex,
    };
    setState(nextState);
    return nextState;
  }

  return state;
}

export const SessionTimeline = forwardRef<
  SessionTimelineHandle,
  SessionTimelineProps
>(function SessionTimeline(
  {
    timeline,
    hasMoreEvents,
    loadingMoreEvents,
    loadMoreError,
    empty,
    thinking,
    onLoadMore,
    onViewAllFiles,
    onFileClick,
    onToolClick,
  },
  ref,
) {
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const loadingRef = useRef(false);
  const [collapsedStepIds, setCollapsedStepIds] = useState<Set<string>>(
    () => new Set(),
  );
  const virtualTimeline = useVirtualTimeline(timeline);

  const loadMore = useCallback(
    async (manual = false) => {
      if (
        !hasMoreEvents ||
        loadingMoreEvents ||
        loadingRef.current ||
        (!manual && loadMoreError)
      ) {
        return;
      }
      loadingRef.current = true;
      try {
        await onLoadMore();
      } finally {
        loadingRef.current = false;
      }
    },
    [hasMoreEvents, loadMoreError, loadingMoreEvents, onLoadMore],
  );
  const throttledLoadMore = useThrottledCallback(
    () => void loadMore(false),
    LOAD_MORE_THROTTLE_MS,
  );

  useImperativeHandle(
    ref,
    () => ({
      scrollToLatest(behavior = "smooth") {
        requestAnimationFrame(() => {
          virtuosoRef.current?.scrollToIndex({
            index: "LAST",
            align: "end",
            behavior,
          });
        });
      },
    }),
    [],
  );

  const handleStepExpandedChange = useCallback(
    (stepId: string, expanded: boolean) => {
      setCollapsedStepIds((current) => {
        const next = new Set(current);
        if (expanded) next.delete(stepId);
        else next.add(stepId);
        return next;
      });
    },
    [],
  );

  const context: TimelineContext = {
    hasMoreEvents,
    loadingMoreEvents,
    loadMoreError,
    empty,
    hasItems: virtualTimeline.data.length > 0,
    thinking,
    loadMore: () => void loadMore(true),
  };

  return (
    <Virtuoso<TimelineItem, TimelineContext>
      className="min-h-0 flex-1"
      data={virtualTimeline.data}
      firstItemIndex={virtualTimeline.firstItemIndex}
      initialTopMostItemIndex={{ index: "LAST", align: "end" }}
      computeItemKey={(_, item) => item.id}
      components={timelineComponents}
      context={context}
      atTopThreshold={80}
      startReached={throttledLoadMore}
      increaseViewportBy={{ top: 400, bottom: 600 }}
      ref={(handle) => {
        virtuosoRef.current = handle;
      }}
      itemContent={(_, item) => (
        <ChatMessage
          className="px-0"
          item={item}
          stepExpanded={
            item.kind === "step" ? !collapsedStepIds.has(item.id) : undefined
          }
          onStepExpandedChange={handleStepExpandedChange}
          onViewAllFiles={onViewAllFiles}
          onFileClick={onFileClick}
          onToolClick={onToolClick}
        />
      )}
    />
  );
});
