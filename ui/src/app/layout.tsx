import React from 'react'
import type {Metadata} from 'next'
import {ThemeProvider} from 'next-themes'
import {SidebarProvider} from '@/components/ui/sidebar'
import {SessionsProvider} from '@/providers/sessions-provider'
import {Toaster} from '@/components/ui/sonner'
import './globals.css'
import {LeftPanel} from '@/components/left-panel'
import {ThemeToggle} from '@/components/theme-toggle'

export const metadata: Metadata = {
  title: 'PeopleAgent',
  description: 'PeopleAgent 是一个行动引擎，它超越了答案的范畴，可以执行任务、自动化工作流程，并扩展您的能力。',
  icons: {
    icon: '/icon.svg',
  },
}

export default function RootLayout(
  {
    children,
  }: Readonly<{
    children: React.ReactNode;
  }>,
) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
    <body className="h-screen overflow-hidden">
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem>
    <SessionsProvider>
      <SidebarProvider
        style={{
          // eslint-disable-next-line @typescript-eslint/ban-ts-comment
          // @ts-expect-error
          '--sidebar-width': '300px',
          '--sidebar-width-icon': '300px',
        }}
      >
        {/* 左侧的面板 */}
        <LeftPanel/>
        {/* 右侧的内容 */}
        <div className="flex-1 bg-background h-screen overflow-hidden grid-bg relative">
          {/* 主题切换按钮 - 右下角固定 */}
          <ThemeToggle />
          {children}
        </div>
      </SidebarProvider>
    </SessionsProvider>
    </ThemeProvider>
    <Toaster position="top-center" richColors/>
    </body>
    </html>
  )
}
