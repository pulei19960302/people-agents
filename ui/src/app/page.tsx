'use client'

import {useRef, useState} from 'react'
import {useRouter} from 'next/navigation'
import {ChatHeader} from '@/components/chat-header'
import {ChatInput, type ChatInputRef} from '@/components/chat-input'
import {SuggestedQuestions} from '@/components/suggested-questions'
import {sessionApi} from '@/lib/api/session'
import type {FileInfo} from '@/lib/api/types'
import {toast} from 'sonner'

export default function Page() {
  const router = useRouter()
  const chatInputRef = useRef<ChatInputRef>(null)
  const [sending, setSending] = useState(false)

  const handleQuestionClick = (question: string) => {
    chatInputRef.current?.setInputText(question)
  }

  const handleSend = async (message: string, files: FileInfo[]) => {
    if (sending) return

    setSending(true)

    try {
      // 1. 创建新会话
      const session = await sessionApi.createSession()
      const sessionId = session.session_id

      // 2. 将消息数据编码到 URL，在详情页发送
      const attachments = files.map((file) => file.id)
      const payload = JSON.stringify({ message, attachments })
      // 使用 Base64 编码避免 URL 特殊字符问题
      const encoded = btoa(encodeURIComponent(payload))
      
      // 3. 跳转到详情页，携带编码后的初始消息
      router.push(`/sessions/${sessionId}?init=${encoded}`)
      
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '创建会话失败'
      toast.error(errorMessage)
      setSending(false)
      throw error
    }
  }

  return (
    <div className="h-full flex flex-col">
      {/* 顶部header */}
      <ChatHeader/>
      {/* 中间对话框 - 垂直居中，视觉上移一个导航栏高度 */}
      <div className="flex-1 flex items-center justify-center px-4 py-6 sm:py-8 -mt-12 sm:-mt-16">
        <div className="w-full max-w-full sm:max-w-[768px] sm:min-w-[390px] mx-auto">
          {/* 对话提示内容 */}
          <div className="mb-4 sm:mb-6 text-center">
            {/* 品牌大图标 */}
            <div className="mx-auto w-16 h-16 sm:w-20 sm:h-20 mb-4 rounded-2xl bg-gradient-to-br from-[#7c5cf0] to-[#6366f1] flex items-center justify-center shadow-lg shadow-primary/20">
              <svg width="40" height="40" viewBox="0 0 64 64" fill="none">
                <g transform="translate(32,32)">
                  <circle cx="0" cy="0" r="20" stroke="rgba(255,255,255,0.25)" stroke-width="1.5" fill="none"/>
                  <polygon points="0,-12 10.4,-6 10.4,6 0,12 -10.4,6 -10.4,-6" 
                           stroke="white" stroke-width="2" fill="none" stroke-linejoin="round"/>
                  <circle cx="0" cy="0" r="3" fill="#06d6d4"/>
                  <circle cx="0" cy="-12" r="2.5" fill="rgba(6,214,212,0.9)"/>
                  <circle cx="10.4" cy="6" r="2.5" fill="rgba(6,214,212,0.9)"/>
                  <circle cx="-10.4" cy="6" r="2.5" fill="rgba(6,214,212,0.9)"/>
                </g>
              </svg>
            </div>
            <div className="gradient-text text-[28px] sm:text-[38px] font-extrabold tracking-tight">
              PeopleAgent
            </div>
            <div className="text-muted-foreground text-base sm:text-lg mt-1.5">智能行动引擎 · 让 AI 为你执行任务</div>
          </div>
          {/* 对话框 */}
          <ChatInput
            ref={chatInputRef}
            className="mb-4 sm:mb-6"
            onSend={handleSend}
            disabled={sending}
          />
          {/* 推荐对话内容 */}
          <SuggestedQuestions onQuestionClick={handleQuestionClick}/>
        </div>
      </div>
    </div>
  )
}
