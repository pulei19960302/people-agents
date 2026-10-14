'use client'

import { useMemo } from 'react'
import type { ToolEvent } from '@/lib/api/types'
import { getToolKind, getFriendlyToolLabel, getArg } from '@/components/tool-use/utils'
import type { ToolKind } from '@/components/tool-use/utils'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import {
  X,
  ArrowDownToLine,
  Terminal,
  Globe,
  Search,
  FileSearch,
  Wrench,
  Bot,
  MonitorPlay,
  type LucideIcon,
} from 'lucide-react'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

export interface ToolPreviewPanelProps {
  tool: ToolEvent
  onClose: () => void
  onJumpToLatest?: () => void
  onOpenVNC?: () => void
}

type ConsoleRecord = { ps1: string; command: string; output: string }

type SearchResultItem = { url: string; title: string; snippet: string }

/* ------------------------------------------------------------------ */
/*  Content extractors                                                 */
/* ------------------------------------------------------------------ */

function getToolContent(tool: ToolEvent): Record<string, unknown> | null {
  const c = tool.content
  if (c && typeof c === 'object' && !Array.isArray(c)) return c as Record<string, unknown>
  return null
}

function getToolDescription(kind: ToolKind): string {
  const map: Record<ToolKind, string> = {
    bash: '终端',
    browser: '浏览器',
    search: '搜索',
    file: '文件',
    mcp: 'MCP',
    a2a: 'Agent',
    message: '消息',
    default: '工具',
  }
  return map[kind]
}

function getToolIcon(kind: ToolKind): LucideIcon {
  const map: Record<ToolKind, LucideIcon> = {
    bash: Terminal,
    browser: Globe,
    search: Search,
    file: FileSearch,
    mcp: Wrench,
    a2a: Bot,
    message: MonitorPlay,
    default: MonitorPlay,
  }
  return map[kind]
}

/* ------------------------------------------------------------------ */
/*  Status indicator                                   */
/* ------------------------------------------------------------------ */

function StatusDot({ active }: { active?: boolean }) {
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${
        active
          ? 'bg-emerald-500 animate-pulse'
          : 'bg-muted-foreground/40'
      }`}
    />
  )
}

/* ------------------------------------------------------------------ */
/*  Jump-to-latest button                              */
/* ------------------------------------------------------------------ */

function JumpToLatestButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-background/80 backdrop-blur text-xs font-medium text-foreground hover:bg-accent shadow-lg border border-border transition-all cursor-pointer"
    >
      <ArrowDownToLine size={12} />
      <span>最新</span>
    </button>
  )
}

/* ------------------------------------------------------------------ */
/*  Sub-previews                                                       */
/* ------------------------------------------------------------------ */

function ShellPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const consoleData = content?.console
  const sessionId = getArg(tool.args, 'session_id')

  const records: ConsoleRecord[] = useMemo(() => {
    if (Array.isArray(consoleData)) return consoleData as ConsoleRecord[]
    return []
  }, [consoleData])

  return (
    <div className="flex flex-col gap-2 p-3 h-full">
      <div className="flex-1 rounded-lg overflow-hidden border border-border bg-card flex flex-col min-h-0">
        <div className="flex items-center gap-1.5 px-3 py-1.5 bg-muted border-b border-border flex-shrink-0">
          <StatusDot active={records.length > 0} />
          <span className="text-xs text-muted-foreground font-mono truncate">
            {sessionId || 'shell'}
          </span>
        </div>
        <ScrollArea className="flex-1">
          <div className="p-3 font-mono text-sm leading-relaxed">
            {records.length > 0 ? records.map((rec, i) => (
              <div key={i} className="mb-2">
                <div className="flex items-baseline gap-1">
                  <span className="text-emerald-500 dark:text-emerald-400">{rec.ps1}</span>
                  <span className="text-foreground">{rec.command}</span>
                </div>
                {rec.output && (
                  <pre className="text-muted-foreground whitespace-pre-wrap break-words mt-0.5 text-xs">{rec.output}</pre>
                )}
              </div>
            )) : (
              <span className="text-muted-foreground text-xs">命令输出将在此显示</span>
            )}
          </div>
        </ScrollArea>
      </div>
    </div>
  )
}

function BrowserPreview({ tool, onOpenVNC }: { tool: ToolEvent; onOpenVNC?: () => void }) {
  const content = getToolContent(tool)
  const screenshot = typeof content?.screenshot === 'string' ? content.screenshot : null
  const url = getArg(tool.args, 'url', 'href', 'link')

  return (
    <div className="flex flex-col gap-2 p-3 h-full">
      {url && (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-md bg-muted border border-border text-xs text-muted-foreground flex-shrink-0">
          <Globe size={12} className="flex-shrink-0" />
          <span className="truncate font-mono">{url}</span>
        </div>
      )}
      <div className="flex-1 rounded-lg overflow-hidden border border-border min-h-0 relative bg-card">
        {screenshot ? (
          <ScrollArea className="h-full">
            <img
              src={screenshot}
              alt="页面截图"
              className="w-full h-auto"
            />
          </ScrollArea>
        ) : (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-muted-foreground">
            <Globe size={24} className="opacity-40" />
            <span className="text-xs">正在加载页面...</span>
          </div>
        )}
        {onOpenVNC && (
          <button
            type="button"
            onClick={onOpenVNC}
            className="absolute bottom-3 right-3 inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md bg-background/90 backdrop-blur text-xs font-medium text-foreground border border-border shadow-lg hover:bg-accent transition-all cursor-pointer z-10"
            aria-label="远程桌面"
          >
            <MonitorPlay size={13} />
            <span>远程桌面</span>
          </button>
        )}
      </div>
    </div>
  )
}

function SearchPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const rawResults = content?.results

  const results: SearchResultItem[] = useMemo(() => {
    if (Array.isArray(rawResults)) return rawResults as SearchResultItem[]
    return []
  }, [rawResults])

  const query = getArg(tool.args, 'query', 'q')

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-1 p-3">
        {query && (
          <div className="text-xs text-muted-foreground mb-3 px-1">
            <span className="text-foreground font-medium">{query}</span>
            <span className="mx-1.5">·</span>
            <span>{results.length} 条结果</span>
          </div>
        )}
        {results.length > 0 ? results.map((item, i) => (
          <a
            key={i}
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className="block px-3 py-2 rounded-md hover:bg-accent transition-colors group"
          >
            <div className="text-xs text-primary/70 truncate mb-0.5 font-mono">{item.url}</div>
            <div className="text-sm font-medium text-foreground group-hover:text-primary transition-colors mb-0.5 line-clamp-1">
              {item.title}
            </div>
            {item.snippet && (
              <div className="text-xs text-muted-foreground line-clamp-2">{item.snippet}</div>
            )}
          </a>
        )) : (
          <div className="flex flex-col items-center justify-center py-12 gap-2 text-muted-foreground">
            <Search size={20} className="opacity-40" />
            <span className="text-xs">暂无结果</span>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

function FileToolPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const fileContent = typeof content?.content === 'string' ? content.content : null
  const filepath = getArg(tool.args, 'filepath', 'path', 'pathname')

  return (
    <div className="flex flex-col gap-2 p-3 h-full">
      <div className="flex-1 rounded-lg overflow-hidden border border-border bg-card flex flex-col min-h-0">
        {filepath && (
          <div className="flex items-center gap-1.5 px-3 py-1.5 bg-muted border-b border-border flex-shrink-0">
            <FileSearch size={12} className="text-muted-foreground flex-shrink-0" />
            <span className="text-xs text-muted-foreground font-mono truncate">{filepath}</span>
          </div>
        )}
        <ScrollArea className="flex-1">
          <pre className="p-3 font-mono text-sm text-foreground whitespace-pre-wrap break-words leading-relaxed">
            {fileContent ?? '文件内容将在此显示'}
          </pre>
        </ScrollArea>
      </div>
    </div>
  )
}

function MCPPreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const result = content?.result

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-3 p-3">
        <div className="flex flex-col gap-1.5">
          <div className="text-xs text-muted-foreground font-medium px-1">调用详情</div>
          <div className="rounded-md border border-border bg-muted/50 p-2.5 text-sm space-y-0.5">
            <div className="flex gap-2">
              <span className="text-muted-foreground text-xs w-14 flex-shrink-0">工具</span>
              <span className="text-foreground font-mono text-xs">{tool.name}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground text-xs w-14 flex-shrink-0">函数</span>
              <span className="text-foreground font-mono text-xs">{tool.function}</span>
            </div>
            {Object.keys(tool.args).length > 0 && (
              <div className="flex gap-2 pt-1">
                <span className="text-muted-foreground text-xs w-14 flex-shrink-0">参数</span>
                <pre className="text-xs text-muted-foreground font-mono whitespace-pre-wrap break-words flex-1">
                  {JSON.stringify(tool.args, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <div className="text-xs text-muted-foreground font-medium px-1">返回值</div>
          <div className="rounded-md border border-border bg-card p-3">
            <pre className="font-mono text-xs text-foreground whitespace-pre-wrap break-words">
              {result != null
                ? (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
                : '执行中...'}
            </pre>
          </div>
        </div>
      </div>
    </ScrollArea>
  )
}

function A2APreview({ tool }: { tool: ToolEvent }) {
  const content = getToolContent(tool)
  const result = content?.a2a_result

  const query = getArg(tool.args, 'query', 'message', 'input')

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-3 p-3">
        <div className="flex flex-col gap-1.5">
          <div className="text-xs text-muted-foreground font-medium px-1">Agent 调用</div>
          <div className="rounded-md border border-border bg-muted/50 p-2.5 text-sm space-y-0.5">
            <div className="flex gap-2">
              <span className="text-muted-foreground text-xs w-14 flex-shrink-0">工具</span>
              <span className="text-foreground font-mono text-xs">{tool.name}</span>
            </div>
            <div className="flex gap-2">
              <span className="text-muted-foreground text-xs w-14 flex-shrink-0">函数</span>
              <span className="text-foreground font-mono text-xs">{tool.function}</span>
            </div>
            {query && (
              <div className="flex gap-2">
                <span className="text-muted-foreground text-xs w-14 flex-shrink-0">指令</span>
                <span className="text-foreground text-xs">{query}</span>
              </div>
            )}
          </div>
        </div>
        <div className="flex flex-col gap-1.5">
          <div className="text-xs text-muted-foreground font-medium px-1">返回值</div>
          <div className="rounded-md border border-border bg-card p-3">
            <pre className="font-mono text-xs text-foreground whitespace-pre-wrap break-words">
              {result != null
                ? (typeof result === 'string' ? result : JSON.stringify(result, null, 2))
                : '执行中...'}
            </pre>
          </div>
        </div>
      </div>
    </ScrollArea>
  )
}

function DefaultPreview({ tool }: { tool: ToolEvent }) {
  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-3 p-3">
        <div className="rounded-md border border-border bg-muted/50 p-2.5 text-sm space-y-0.5">
          <div className="flex gap-2">
            <span className="text-muted-foreground text-xs w-14 flex-shrink-0">工具</span>
            <span className="text-foreground font-mono text-xs">{tool.name}</span>
          </div>
          <div className="flex gap-2">
            <span className="text-muted-foreground text-xs w-14 flex-shrink-0">函数</span>
            <span className="text-foreground font-mono text-xs">{tool.function}</span>
          </div>
        </div>
        {tool.content != null && (
          <div className="rounded-md border border-border bg-card p-3">
            <pre className="font-mono text-xs text-foreground whitespace-pre-wrap break-words">
              {typeof tool.content === 'string' ? tool.content : JSON.stringify(tool.content, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </ScrollArea>
  )
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export function ToolPreviewPanel({
  tool,
  onClose,
  onJumpToLatest,
  onOpenVNC,
}: ToolPreviewPanelProps) {
  const kind = getToolKind(tool)
  const label = getFriendlyToolLabel(tool)
  const ToolIcon = getToolIcon(kind)
  const toolDesc = getToolDescription(kind)
  const isActive = !tool.content

  return (
    <div className="flex flex-col h-full rounded-xl glass shadow-2xl overflow-hidden border border-border">
      {/* Header — compact single row */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-border bg-muted flex-shrink-0">
        <StatusDot active={isActive} />
        <ToolIcon size={14} className="flex-shrink-0 text-primary" />
        <span className="text-xs text-muted-foreground flex-shrink-0">{toolDesc}</span>
        <span className="text-xs text-muted-foreground/50">/</span>
        <span className="text-xs text-foreground truncate flex-1">{label}</span>
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={onClose}
          aria-label="关闭"
          className="cursor-pointer flex-shrink-0"
        >
          <X size={15} />
        </Button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden relative">
        {kind === 'bash' && <ShellPreview tool={tool} />}
        {kind === 'browser' && <BrowserPreview tool={tool} onOpenVNC={onOpenVNC} />}
        {kind === 'search' && <SearchPreview tool={tool} />}
        {kind === 'file' && <FileToolPreview tool={tool} />}
        {kind === 'mcp' && <MCPPreview tool={tool} />}
        {kind === 'a2a' && <A2APreview tool={tool} />}
        {(kind === 'default' || kind === 'message') && <DefaultPreview tool={tool} />}

        {onJumpToLatest && (
          <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10">
            <JumpToLatestButton onClick={onJumpToLatest} />
          </div>
        )}
      </div>
    </div>
  )
}
