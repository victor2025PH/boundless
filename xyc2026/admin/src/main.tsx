import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { AppProvider, useApp } from './context'
import Layout from './layout/Layout'
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import ReportsPage from './pages/ReportsPage'
import FeedbackPage from './pages/FeedbackPage'
import BlacklistPage from './pages/BlacklistPage'
import ReportersPage from './pages/ReportersPage'
import TransactionsPage from './pages/TransactionsPage'
import RiskTrendPage from './pages/RiskTrendPage'
import AlertsPage from './pages/AlertsPage'
import LookupPage from './pages/LookupPage'
import './styles.css'

function AppRoutes() {
  const { authed } = useApp()

  if (!authed) return <LoginPage />

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="feedback" element={<FeedbackPage />} />
        <Route path="blacklist" element={<BlacklistPage />} />
        <Route path="reporters" element={<ReportersPage />} />
        <Route path="transactions" element={<TransactionsPage />} />
        <Route path="risk-trend" element={<RiskTrendPage />} />
        <Route path="alerts" element={<AlertsPage />} />
        <Route path="lookup" element={<LookupPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <AppProvider>
        <AppRoutes />
      </AppProvider>
    </BrowserRouter>
  </StrictMode>
)
