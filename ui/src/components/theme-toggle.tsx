'use client'

import { useTheme } from 'next-themes'
import { Sun, Moon, Laptop } from 'lucide-react'
import { useEffect, useState } from 'react'

export function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const [mounted, setMounted] = useState(false)

  useEffect(() => setMounted(true), [])
  if (!mounted) return null

  const cycle = () => {
    if (theme === 'light') setTheme('dark')
    else if (theme === 'dark') setTheme('system')
    else setTheme('light')
  }

  const icon =
    theme === 'dark' ? <Moon size={16} /> :
    theme === 'system' ? <Laptop size={16} /> :
    <Sun size={16} />

  const label =
    theme === 'dark' ? '暗色' :
    theme === 'system' ? '跟随系统' :
    '亮色'

  return (
    <button
      onClick={cycle}
      title={`当前：${label}，点击切换`}
      className="
        fixed bottom-4 right-4 z-50
        h-9 w-9 rounded-lg
        flex items-center justify-center
        bg-card/80 backdrop-blur-sm
        border border-border
        text-muted-foreground hover:text-foreground
        hover:bg-accent
        transition-all duration-200
        shadow-sm hover:shadow-md
      "
    >
      {icon}
    </button>
  )
}
