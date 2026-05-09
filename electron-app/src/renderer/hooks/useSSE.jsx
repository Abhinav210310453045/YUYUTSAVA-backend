import { createContext, useContext, useReducer, useEffect, useRef } from 'react'
import { SSEClient } from '../api/sse'

const MAX_LOG_LINES = 2000

const SSEContext = createContext(null)

function reducer(state, action) {
  switch (action.type) {
    case 'CONNECTED':
      return { ...state, connected: true }
    case 'DISCONNECTED':
      return { ...state, connected: false }
    case 'PROPOSAL': {
      const p = action.payload.proposal
      const proposals = new Map(state.proposals)
      proposals.set(p.proposal_id, p)
      return { ...state, proposals, pendingCount: proposals.size + state.asks.size }
    }
    case 'ASK': {
      const a = action.payload.ask
      const asks = new Map(state.asks)
      asks.set(a.ask_id, a)
      return { ...state, asks, pendingCount: state.proposals.size + asks.size }
    }
    case 'REMOVE_PROPOSAL': {
      const proposals = new Map(state.proposals)
      proposals.delete(action.id)
      return { ...state, proposals, pendingCount: proposals.size + state.asks.size }
    }
    case 'REMOVE_ASK': {
      const asks = new Map(state.asks)
      asks.delete(action.id)
      return { ...state, asks, pendingCount: state.proposals.size + asks.size }
    }
    case 'LOG_LINE': {
      const logLines = [...state.logLines, action.line]
      return { ...state, logLines: logLines.length > MAX_LOG_LINES ? logLines.slice(-MAX_LOG_LINES) : logLines }
    }
    default:
      return state
  }
}

const initialState = {
  proposals: new Map(),
  asks: new Map(),
  logLines: [],
  connected: false,
  pendingCount: 0,
}

export function SSEProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initialState)
  const clientRef = useRef(null)

  useEffect(() => {
    const client = new SSEClient({
      onConnected: () => dispatch({ type: 'CONNECTED' }),
      onDisconnected: () => dispatch({ type: 'DISCONNECTED' }),
      onProposal: (data) => dispatch({ type: 'PROPOSAL', payload: data }),
      onAsk: (data) => dispatch({ type: 'ASK', payload: data }),
      onEvent: (data) => {
        const kind = data.kind || 'log'
        let text = ''
        if (kind === 'token') text = data.data?.token || ''
        else if (kind === 'tool_call') text = `${data.data?.name || ''}(${JSON.stringify(data.data?.input || {})})`
        else if (kind === 'tool_result') text = `${data.data?.name || ''}: ${JSON.stringify(data.data?.output ?? '').slice(0, 120)}`
        else if (kind === 'timeline') text = data.data?.text || data.data?.summary || JSON.stringify(data.data)
        else text = data.data?.text || data.data?.message || JSON.stringify(data.data)

        dispatch({ type: 'LOG_LINE', line: { kind, text, ts: data.data?.ts || Date.now() / 1000 } })
      },
    })
    clientRef.current = client
    client.connect()
    return () => client.disconnect()
  }, [])

  // Update tray badge when pending count changes
  useEffect(() => {
    window.electronAPI?.setProposalCount(state.pendingCount)
  }, [state.pendingCount])

  const removeProposal = (id) => dispatch({ type: 'REMOVE_PROPOSAL', id })
  const removeAsk = (id) => dispatch({ type: 'REMOVE_ASK', id })

  return (
    <SSEContext.Provider value={{ ...state, removeProposal, removeAsk }}>
      {children}
    </SSEContext.Provider>
  )
}

export function useSSE() {
  return useContext(SSEContext)
}
