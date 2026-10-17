import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import ApprovalWindow from './ApprovalWindow'
import './styles.css'

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 1500 } } })
const Surface=window.location.hash==='#approval'?ApprovalWindow:App
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><QueryClientProvider client={client}><Surface/></QueryClientProvider></React.StrictMode>)

