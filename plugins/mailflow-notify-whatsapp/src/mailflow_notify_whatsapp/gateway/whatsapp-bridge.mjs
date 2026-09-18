// whatsapp-bridge: WhatsApp gateway bridge for MailFlow based on Baileys
// (https://github.com/WhiskeySockets/Baileys).
//
// Exposes the MailFlow gateway HTTP contract:
//   GET  /health -> {"logged_in": bool, "status": "pending"|"scanning"|"logged_in"|"error", "error": ...}
//   GET  /qr     -> {"status": ..., "qrcode": "<base64 png>", "error": ...}
//   POST /send   -> {"to": {"type": "contact"|"group", "name": ...}, "text": ...}
//
// Chat commands: incoming text messages are forwarded to the MailFlow bot
// endpoint (MAILFLOW_BOT_URL, the local mailflow.bot_server command
// dispatcher); every reply page it returns is sent back to the same chat.
// MAILFLOW_PROVIDER / MAILFLOW_INSTANCE carry the gateway identity so
// subscription commands target the right instance.
//
// Login is scan-to-login: the Baileys multi-file auth state under ./auth
// keeps the session across restarts — no platform token required. The QR is
// rendered to a PNG so the TUI can display it directly.
//
// Run:  GATEWAY_PORT=8898 MAILFLOW_BOT_URL=http://127.0.0.1:18789/bot/message node whatsapp-bridge.mjs

import http from 'node:http'

import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'

const PORT = Number(process.env.GATEWAY_PORT || 8898)
const BOT_URL = process.env.MAILFLOW_BOT_URL || ''
const PROVIDER = process.env.MAILFLOW_PROVIDER || 'whatsapp'
const INSTANCE = process.env.MAILFLOW_INSTANCE || ''
const AUTH_DIR = './auth'
const MAX_BODY_BYTES = 1 << 20
// Baileys logs through pino; the bridge's own stdout must stay readable
// (the provisioner tails it for diagnostics), so keep the SDK logger quiet.
const logger = pino({ level: 'silent' })

const state = { status: 'pending', qr: '', error: '', started: Date.now() }
let sock = null
let reconnectDelayMs = 1000

function log(message) {
  console.log(`[whatsapp-bridge] ${message}`)
}

function snapshot() {
  return { status: state.status, qr: state.qr, error: state.error, started: state.started }
}

function writeJSON(res, code, payload) {
  const body = JSON.stringify(payload)
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
  })
  res.end(body)
}

// --- inbound chat commands ---------------------------------------------------

// A text message can arrive wrapped (ephemeral / view-once envelopes); unwrap
// a few levels before looking for the text payload.
function unwrap(message) {
  let node = message
  for (let depth = 0; depth < 4 && node; depth += 1) {
    const wrapper = node.ephemeralMessage || node.viewOnceMessage || node.viewOnceMessageV2
    if (!wrapper || !wrapper.message) return node
    node = wrapper.message
  }
  return node
}

function extractText(message) {
  const inner = unwrap(message)
  if (!inner) return ''
  if (typeof inner.conversation === 'string') return inner.conversation
  if (inner.extendedTextMessage && typeof inner.extendedTextMessage.text === 'string') {
    return inner.extendedTextMessage.text
  }
  return ''
}

async function sendText(chatId, text) {
  if (!sock || state.status !== 'logged_in') throw new Error('not logged in')
  await sock.sendMessage(chatId, { text })
}

async function dispatch(text, sender, chatId, chatType) {
  if (!BOT_URL) return
  let pages = []
  try {
    const response = await fetch(BOT_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        sender,
        chat_id: chatId,
        chat_type: chatType,
        provider: PROVIDER,
        instance_id: INSTANCE,
      }),
    })
    const data = await response.json().catch(() => null)
    const reply = data ? data.reply : null
    // bot_server answers with one string or a page list; send every page in
    // order, exactly as returned (pages are already fitted for chat transport)
    if (Array.isArray(reply)) pages = reply
    else if (typeof reply === 'string' && reply.trim()) pages = [reply]
  } catch (err) {
    log(`bot dispatch failed: ${err && err.message ? err.message : err}`)
    return
  }
  for (const page of pages) {
    const chunk = page === null || page === undefined ? '' : String(page)
    if (!chunk.trim()) continue
    try {
      await sendText(chatId, chunk)
    } catch (err) {
      log(`reply to ${chatId} failed: ${err && err.message ? err.message : err}`)
    }
  }
}

function handleMessages(event) {
  if (event.type !== 'notify') return
  for (const message of event.messages || []) {
    if (!message || !message.message) continue
    if (message.key && message.key.fromMe) continue
    const chatId = (message.key && message.key.remoteJid) || ''
    if (!chatId) continue
    const text = extractText(message.message)
    if (!text) continue
    const isGroup = chatId.endsWith('@g.us')
    const sender = isGroup ? (message.key && message.key.participant) || chatId : chatId
    dispatch(text, sender, chatId, isGroup ? 'group' : 'private').catch((err) => {
      log(`dispatch failed: ${err && err.message ? err.message : err}`)
    })
  }
}

// --- Baileys session ---------------------------------------------------------

async function handleConnectionUpdate(update) {
  const { connection, lastDisconnect, qr } = update
  if (qr) {
    try {
      const dataUrl = await QRCode.toDataURL(qr, { width: 256 })
      // the contract wants the bare base64 PNG, without the data: prefix
      state.qr = dataUrl.replace(/^data:image\/png;base64,/, '')
      state.status = 'scanning'
      state.error = ''
      log('login QR ready — scan it with WhatsApp')
    } catch (err) {
      log(`QR render failed: ${err && err.message ? err.message : err}`)
    }
  }
  if (connection === 'open') {
    state.status = 'logged_in'
    state.qr = ''
    state.error = ''
    reconnectDelayMs = 1000
    log('logged in')
    return
  }
  if (connection !== 'close') return
  const output = lastDisconnect && lastDisconnect.error ? lastDisconnect.error.output : null
  const statusCode = output ? output.statusCode : undefined
  if (statusCode === DisconnectReason.loggedOut) {
    // the phone unlinked this session: reconnecting would loop forever
    state.status = 'error'
    state.qr = ''
    state.error =
      'logged out on the phone — delete the session under the instance dir and scan the QR again'
    log('logged out; not reconnecting')
    return
  }
  const delay = reconnectDelayMs
  reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000)
  state.status = 'pending'
  state.qr = ''
  state.error = `connection closed (code ${statusCode}); reconnecting in ${Math.round(delay / 1000)}s`
  log(state.error)
  setTimeout(() => {
    connect().catch((err) => log(`reconnect failed: ${err && err.message ? err.message : err}`))
  }, delay)
}

async function connect() {
  const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
  sock = makeWASocket({
    auth: authState,
    printQRInTerminal: false,
    browser: ['MailFlow', 'Desktop', '1.0'],
    logger,
  })
  sock.ev.on('creds.update', saveCreds)
  sock.ev.on('connection.update', (update) => {
    handleConnectionUpdate(update).catch((err) =>
      log(`connection update failed: ${err && err.message ? err.message : err}`),
    )
  })
  sock.ev.on('messages.upsert', handleMessages)
}

// --- HTTP contract -----------------------------------------------------------

function resolveJid(to) {
  const name = String((to && to.name) || '').trim()
  if (!name) return ''
  // a full JID passes through; a bare id gets the suffix its target type needs
  if (name.includes('@')) return name
  return to.type === 'group' ? `${name}@g.us` : `${name}@s.whatsapp.net`
}

async function handleSend(req, res) {
  let raw = ''
  for await (const chunk of req) {
    raw += chunk
    if (raw.length > MAX_BODY_BYTES) {
      writeJSON(res, 413, { ok: false, error: 'request too large' })
      return
    }
  }
  let parsed = null
  try {
    parsed = JSON.parse(raw || '{}')
  } catch {
    writeJSON(res, 400, { ok: false, error: 'bad json' })
    return
  }
  const body = parsed && typeof parsed === 'object' ? parsed : {}
  const text = typeof body.text === 'string' ? body.text : ''
  if (!text) {
    writeJSON(res, 400, { ok: false, error: 'text required' })
    return
  }
  const jid = resolveJid(body.to)
  if (!jid) {
    writeJSON(res, 400, { ok: false, error: 'target required' })
    return
  }
  if (state.status !== 'logged_in') {
    writeJSON(res, 503, { ok: false, error: 'not logged in' })
    return
  }
  try {
    await sendText(jid, text)
    writeJSON(res, 200, { ok: true })
  } catch (err) {
    writeJSON(res, 500, { ok: false, error: err && err.message ? err.message : String(err) })
  }
}

const server = http.createServer((req, res) => {
  const path = (req.url || '').split('?')[0]
  if (req.method === 'GET' && path === '/health') {
    const current = snapshot()
    writeJSON(res, 200, {
      logged_in: current.status === 'logged_in',
      status: current.status,
      error: current.error,
    })
    return
  }
  if (req.method === 'GET' && path === '/qr') {
    const current = snapshot()
    if (current.status === 'logged_in') {
      writeJSON(res, 200, { status: 'logged_in' })
      return
    }
    if (current.status === 'error') {
      writeJSON(res, 200, { status: 'error', error: current.error })
      return
    }
    if (current.status === 'scanning' && current.qr) {
      writeJSON(res, 200, { status: 'scanning', qrcode: current.qr })
      return
    }
    if (Date.now() - current.started > 60000) {
      writeJSON(res, 200, {
        status: 'error',
        error: `${current.error} (no QR within 60s)`.trim(),
      })
      return
    }
    writeJSON(res, 200, { status: 'pending' })
    return
  }
  if (req.method === 'POST' && path === '/send') {
    handleSend(req, res).catch((err) =>
      log(`send handler failed: ${err && err.message ? err.message : err}`),
    )
    return
  }
  writeJSON(res, 404, { ok: false, error: 'not found' })
})

process.on('unhandledRejection', (reason) => {
  log(`unhandled rejection: ${reason && reason.message ? reason.message : reason}`)
})

connect().catch((err) => {
  state.status = 'error'
  state.error = `session start failed: ${err && err.message ? err.message : err}`
  log(state.error)
})

server.listen(PORT, '127.0.0.1', () => {
  log(`listening on 127.0.0.1:${PORT}`)
})
