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

import makeWASocket, { DisconnectReason, useMultiFileAuthState, downloadMediaMessage } from '@whiskeysockets/baileys'
import pino from 'pino'
import QRCode from 'qrcode'

const PORT = Number(process.env.GATEWAY_PORT || 8898)
const BOT_URL = process.env.MAILFLOW_BOT_URL || ''
const PROVIDER = process.env.MAILFLOW_PROVIDER || 'whatsapp'
const INSTANCE = process.env.MAILFLOW_INSTANCE || ''
const AUTH_DIR = './auth'
const MAX_BODY_BYTES = 24 << 20
// Baileys logs through pino; the bridge's own stdout must stay readable
// (the provisioner tails it for diagnostics), so keep the SDK logger quiet.
const logger = pino({ level: 'silent' })

const state = { status: 'pending', qr: '', error: '', started: Date.now() }
let sock = null
let reconnectDelayMs = 1000
let reconnectTimer = null
let connecting = false

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
  return inner.documentMessage?.caption || inner.imageMessage?.caption || ''
}

async function sendText(chatId, text) {
  if (!sock || state.status !== 'logged_in') throw new Error('not logged in')
  await sock.sendMessage(chatId, { text })
}

async function dispatch(text, sender, chatId, chatType, requestId, mentioned = false, files = [], fileError = false) {
  if (!BOT_URL) return
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
        request_id: requestId || "",
        mentioned,
        files,
        file_error: fileError,
        stream: true,
      }),
    })
    if (!response.ok) throw new Error(`MailFlow HTTP ${response.status}`)
    const decoder = new TextDecoder()
    let pending = ''
    async function deliver(line) {
      if (!line.trim()) return
      const data = JSON.parse(line)
      const pages = Array.isArray(data.reply) ? data.reply : [data.reply]
      for (const page of pages) {
        if (typeof page === 'string' && page.trim()) await sendText(chatId, page)
      }
    }
    for await (const chunk of response.body) {
      pending += decoder.decode(chunk, { stream: true })
      let newline
      while ((newline = pending.indexOf('\n')) >= 0) {
        await deliver(pending.slice(0, newline))
        pending = pending.slice(newline + 1)
      }
    }
    pending += decoder.decode()
    if (pending.trim()) await deliver(pending)
  } catch (err) {
    log(`command connection failed: ${err && err.message ? err.message : err}`)
    if (/^[^\p{L}\p{N}_]*mailflow(?:\s|$)/iu.test(text) && process.env.MAILFLOW_COMMAND_ERROR) {
      await sendText(chatId, process.env.MAILFLOW_COMMAND_ERROR).catch((error) =>
        log(`error reply failed: ${error && error.message ? error.message : error}`),
      )
    }
  }
}

async function handleMessages(event) {
  if (event.type !== 'notify') return
  for (const message of event.messages || []) {
    if (!message || !message.message) continue
    if (message.key && message.key.fromMe) continue
    const chatId = (message.key && message.key.remoteJid) || ''
    if (!chatId) continue
    const text = extractText(message.message)
    const inner = unwrap(message.message)
    const media = inner?.documentMessage || inner?.imageMessage
    if (!text && !media) continue
    const isGroup = chatId.endsWith('@g.us')
    const sender = isGroup ? (message.key && message.key.participant) || chatId : chatId
    const context = message.message.extendedTextMessage?.contextInfo || {}
    const ownId = (sock?.user?.id || '').replace(/:\d+@/, '@')
    const mentioned = (context.mentionedJid || []).some(id => id.replace(/:\d+@/, '@') === ownId)
    const instruction = mentioned ? text.replace(/@\d+/g, '').trim() : text
    const files = []
    let fileError = false
    if (media) {
      try {
        const stream = await downloadMediaMessage(message, 'stream', {}, { logger, reuploadRequest: sock.updateMediaMessage })
        const chunks = []
        let size = 0
        for await (const chunk of stream) {
          size += chunk.length
          if (size > 4000000) { stream.destroy(); throw new Error('attachment too large') }
          chunks.push(chunk)
        }
        files.push({filename: media.fileName || 'image.jpg', content_type: media.mimetype || 'application/octet-stream', data: Buffer.concat(chunks).toString('base64')})
      } catch { fileError = true }
    }
    dispatch(instruction, sender, chatId, isGroup ? 'group' : 'private', message.key && message.key.id, mentioned, files, fileError).catch((err) => {
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
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    connect().catch((err) => log(`reconnect failed: ${err && err.message ? err.message : err}`))
  }, delay)
}

async function connect() {
  if (connecting) return
  connecting = true
  try {
    const previous = sock
    if (previous) previous.ev.removeAllListeners?.()
    const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    sock = makeWASocket({
      auth: authState,
      printQRInTerminal: false,
      browser: ['MailFlow', 'Desktop', '1.0'],
      logger,
    })
    const current = sock
    current.ev.on('creds.update', saveCreds)
    current.ev.on('connection.update', (update) => {
      if (sock !== current) return
      handleConnectionUpdate(update).catch((err) =>
        log(`connection update failed: ${err && err.message ? err.message : err}`),
      )
    })
    current.ev.on('messages.upsert', (event) => { if (sock === current) handleMessages(event) })
  } catch (err) {
    state.status = 'pending'
    state.error = 'connection failed; retrying'
    if (reconnectTimer) clearTimeout(reconnectTimer)
    const delay = reconnectDelayMs
    reconnectDelayMs = Math.min(reconnectDelayMs * 2, 30000)
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      connect().catch((error) => log(`reconnect failed: ${error && error.message ? error.message : error}`))
    }, delay)
    throw err
  } finally {
    connecting = false
  }
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
  if (!text && !body.image && !body.file) {
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
    if (body.file) {
      if (!sock) throw new Error('not logged in')
      await sock.sendMessage(jid, { document: Buffer.from(body.file, 'base64'), fileName: body.filename || 'attachment', mimetype: body.content_type || 'application/octet-stream' })
    } else if (body.image) {
      if (!sock) throw new Error('not logged in')
      await sock.sendMessage(jid, { image: Buffer.from(body.image, 'base64'), mimetype: body.content_type || 'image/png' })
    } else {
      await sendText(jid, text)
    }
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
