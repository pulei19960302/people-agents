'use client'

import Link from 'next/link'
import {SidebarTrigger, useSidebar} from '@/components/ui/sidebar'
import {PeopleAgentSettings} from '@/components/people-agent-settings'

export function ChatHeader() {
  const {open, isMobile} = useSidebar()

  return (
    <header className="flex justify-between items-center w-full py-2 px-4 z-50">
      {/* 左侧操作&logo */}
      <div className="flex items-center gap-2">
        {/* 面板操作按钮: 关闭面板&移动端下会显示 */}
        {(!open || isMobile) && <SidebarTrigger className="cursor-pointer"/>}
        <Link href="/" className="flex items-center gap-2 px-3 py-1.5 rounded-lg cursor-pointer border border-border transition-all duration-200 hover:bg-accent hover:border-primary/30">
          {/* 品牌图标 */}
          <svg width="24" height="24" viewBox="0 0 64 64" fill="none" className="shrink-0">
            <defs>
              <linearGradient id="logo-bg" x1="0%" y1="0%" x2="100%" y2="100%">
                <stop offset="0%" stopColor="#7c5cf0"/>
                <stop offset="100%" stopColor="#6366f1"/>
              </linearGradient>
              <linearGradient id="logo-accent" x1="0%" y1="100%" x2="100%" y2="0%">
                <stop offset="0%" stopColor="#06d6d4"/>
                <stop offset="100%" stopColor="#7c5cf0"/>
              </linearGradient>
            </defs>
            <rect width="24" height="24" rx="6" fill="url(#logo-bg)"/>
            <g transform="translate(12,12)">
              <circle cx="0" cy="0" r="7.5" stroke="rgba(255,255,255,0.25)" stroke-width="1" fill="none"/>
              <polygon points="0,-4.5 3.9,-2.25 3.9,2.25 0,4.5 -3.9,2.25 -3.9,-2.25" 
                       stroke="white" stroke-width="1.5" fill="none" stroke-linejoin="round"/>
              <circle cx="0" cy="0" r="1.8" fill="url(#logo-accent)"/>
              <circle cx="0" cy="-4.5" r="1.2" fill="rgba(6,214,212,0.9)"/>
              <circle cx="3.9" cy="2.25" r="1.2" fill="rgba(6,214,212,0.9)"/>
              <circle cx="-3.9" cy="2.25" r="1.2" fill="rgba(6,214,212,0.9)"/>
            </g>
          </svg>
          <span className="text-sm font-bold gradient-text leading-none">PeopleAgent</span>
        </Link>
      </div>
      {/* 右侧设置模态窗 */}
      <PeopleAgentSettings/>
    </header>
  )
}