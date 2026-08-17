import { createContext, useContext, type ReactNode } from 'react'
import { useAuth } from './hooks/useAuth'
import { useToast, type Toast } from './hooks/useToast'

interface AppContextType {
  authed: boolean
  login: (key: string) => void
  logout: () => void
  toasts: Toast[]
  addToast: (msg: string, type?: Toast['type']) => void
  removeToast: (id: number) => void
}

const AppContext = createContext<AppContextType>(null!)

export function AppProvider({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const toast = useToast()
  return (
    <AppContext.Provider value={{ ...auth, ...toast }}>
      {children}
    </AppContext.Provider>
  )
}

export function useApp() {
  return useContext(AppContext)
}
