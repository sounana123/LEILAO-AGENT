/**
 * Local WhatsApp Web bridge for the auction digest.
 *
 * A tiny HTTP server that keeps a WhatsApp Web session alive and exposes:
 *
 *   GET  /status   -> { connected, qr, pairing_code, me, last_error, ... }
 *   POST /send     -> { text } or { image, mimetype, caption } (optional "to")
 *   POST /pair     -> { phone } -> pairing code (alternative to the QR)
 *   POST /logout   -> unlinks the current session
 *   GET  /health   -> { ok: true }
 *
 * The session is stored on disk, so the QR code is only needed once.  Messages
 * go to your own "Message yourself" chat unless a recipient is provided.
 *
 * This uses the unofficial WhatsApp Web protocol (Baileys).  It is a personal
 * convenience, not an official API.
 */

const http = require('http')
const path = require('path')
const fs = require('fs')

const QRCode = require('qrcode')
const pino = require('pino')
const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  Browsers,
} = require('@whiskeysockets/baileys')

let BAILEYS_VERSION = ''
try {
  BAILEYS_VERSION = require('@whiskeysockets/baileys/package.json').version
} catch (error) {
  BAILEYS_VERSION = 'unknown'
}

// ------------------------------------------------------------------ arguments
function arg(name, fallback) {
  const prefix = `--${name}=`
  const prefixed = process.argv.find((value) => value.startsWith(prefix))
  if (prefixed) return prefixed.slice(prefix.length)
  const index = process.argv.indexOf(`--${name}`)
  if (index !== -1 && process.argv[index + 1]) return process.argv[index + 1]
  return fallback
}

const PORT = Number(arg('port', process.env.PORT || 8099))
const HOST = arg('host', '127.0.0.1')
const AUTH_DIR = arg('auth', process.env.AUTH_DIR || path.join(__dirname, 'auth'))
const VERBOSE = arg('verbose', '') === 'true'

if (!fs.existsSync(AUTH_DIR)) fs.mkdirSync(AUTH_DIR, { recursive: true })

// --------------------------------------------------------------------- state
let sock = null
let connected = false
let me = null
let lastQr = null
let lastQrAt = 0
let pairingCode = null
let pairingCodeAt = 0
let lastError = ''
let starting = false
let stopped = false
let reconnectTimer = null
let reconnectDelay = 2000
let generation = 0
let waVersion = ''

const log = (...parts) => console.log(new Date().toISOString(), ...parts)

function publicState() {
  const qrFresh = lastQr && Date.now() - lastQrAt < 120000
  return {
    connected,
    me: me
      ? {
          id: me.id,
          name: me.name || '',
          number: String(me.id || '').split(':')[0].split('@')[0],
        }
      : null,
    qr: connected || !qrFresh ? null : lastQr,
    qr_age_seconds: lastQr ? Math.round((Date.now() - lastQrAt) / 1000) : null,
    pairing_code: connected ? null : pairingCode,
    pairing_code_age_seconds: pairingCode
      ? Math.round((Date.now() - pairingCodeAt) / 1000)
      : null,
    last_error: lastError,
    auth_dir: AUTH_DIR,
    baileys_version: BAILEYS_VERSION,
    wa_version: waVersion,
  }
}

function clearQr() {
  lastQr = null
  lastQrAt = 0
}

function clearPairingCode() {
  pairingCode = null
  pairingCodeAt = 0
}

function statusCodeOf(lastDisconnect) {
  const error = lastDisconnect && lastDisconnect.error
  const output = error && error.output
  return output && output.statusCode ? output.statusCode : undefined
}

function wipeAuth() {
  try {
    fs.rmSync(AUTH_DIR, { recursive: true, force: true })
  } catch (error) {
    log('could not clear auth dir:', error.message)
  }
  fs.mkdirSync(AUTH_DIR, { recursive: true })
}

// -------------------------------------------------------------------- session
async function connect() {
  if (starting || stopped) return
  starting = true
  const gen = ++generation

  // Always dispose the previous socket first: keeping two live sessions is what
  // produces the 401/515 ping-pong after a failed QR scan.
  if (sock) {
    try {
      sock.ev.removeAllListeners('connection.update')
      sock.ev.removeAllListeners('creds.update')
    } catch (error) {
      /* ignore */
    }
    try {
      sock.end(undefined)
    } catch (error) {
      /* ignore */
    }
    sock = null
  }

  try {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    let version
    try {
      const latest = await fetchLatestBaileysVersion()
      version = latest.version
      waVersion = latest.version.join('.')
      log(
        `Baileys ${BAILEYS_VERSION}, WhatsApp Web ${waVersion}` +
          (latest.isLatest ? '' : ' (a newer Baileys is available)')
      )
    } catch (error) {
      log('could not fetch WhatsApp version, using bundled default:', error.message)
    }

    const socket = makeWASocket({
      version,
      auth: state,
      logger: pino({ level: VERBOSE ? 'info' : 'silent' }),
      printQRInTerminal: false,
      browser: Browsers.ubuntu('Chrome'),
      syncFullHistory: false,
      markOnlineOnConnect: false,
      connectTimeoutMs: 60000,
      defaultQueryTimeoutMs: 60000,
      keepAliveIntervalMs: 30000,
      retryRequestDelayMs: 2000,
    })
    sock = socket

    socket.ev.on('creds.update', () => {
      saveCreds().catch((error) => log('could not save credentials:', error.message))
    })

    socket.ev.on('connection.update', async (update) => {
      if (gen !== generation) return // event from a socket we already dropped
      const { connection, lastDisconnect, qr } = update

      if (qr) {
        if (pairingCode) {
          // A pairing code was requested; the QR is no longer needed.
          clearQr()
        } else {
          try {
            lastQr = await QRCode.toDataURL(qr, { margin: 1, width: 320 })
            lastQrAt = Date.now()
            reconnectDelay = 2000
            log('QR ready - scan it from the auction page')
          } catch (error) {
            log('failed to render QR:', error.message)
          }
        }
      }

      if (connection === 'open') {
        connected = true
        me = socket.user
        clearQr()
        clearPairingCode()
        lastError = ''
        reconnectDelay = 2000
        log('connected as', me && me.id)
      }

      if (connection === 'close') {
        connected = false
        me = null
        clearQr()
        clearPairingCode()

        const statusCode = statusCodeOf(lastDisconnect)
        lastError = `connection closed (${statusCode || 'unknown'})`
        log('disconnected:', lastError)

        try {
          socket.end(undefined)
        } catch (error) {
          /* already closed */
        }

        if (statusCode === DisconnectReason.loggedOut || statusCode === 401) {
          // Stale credentials can never link again: start from a clean session
          // so the next QR is scannable.
          wipeAuth()
          lastError = 'session unlinked - a new QR code is on the way'
          reconnectDelay = 1000
          scheduleReconnect()
          return
        }

        if (statusCode === 403) {
          lastError = 'WhatsApp refused this device - press Unlink and scan a fresh QR'
          reconnectDelay = 5000
          scheduleReconnect()
          return
        }

        if (statusCode === DisconnectReason.restartRequired || statusCode === 515) {
          log('restart required - reconnecting immediately')
          reconnectDelay = 500
          scheduleReconnect()
          return
        }

        if (
          statusCode === DisconnectReason.timedOut ||
          statusCode === 408 ||
          statusCode === DisconnectReason.connectionClosed
        ) {
          lastError = 'QR code expired - a new one will appear in a moment'
          reconnectDelay = 500
          scheduleReconnect()
          return
        }

        if (statusCode === DisconnectReason.connectionReplaced) {
          lastError = 'this session was opened somewhere else'
          reconnectDelay = 5000
          scheduleReconnect()
          return
        }

        scheduleReconnect()
      }
    })
  } catch (error) {
    lastError = `${error.name || 'Error'}: ${error.message}`
    log('connect failed:', lastError)
    scheduleReconnect()
  } finally {
    starting = false
  }
}

function scheduleReconnect() {
  if (stopped || reconnectTimer) return
  const delay = reconnectDelay
  reconnectDelay = Math.min(Math.max(reconnectDelay * 2, 2000), 30000)
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null
    attemptReconnect()
  }, delay)
}

async function attemptReconnect() {
  if (stopped) return
  if (starting) {
    // The previous socket is still being created; retry shortly instead of
    // dropping the reconnect on the floor.
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      attemptReconnect()
    }, 500)
    return
  }
  connect()
}

async function sendMessage(content, to) {
  if (!sock || !sock.user || !connected) {
    throw new Error('not linked yet - scan the QR code first')
  }
  let jid
  if (to) {
    const digits = String(to).replace(/[^0-9]/g, '')
    if (!digits) throw new Error('invalid recipient: ' + to)
    if (digits.length < 10) {
      throw new Error('recipient needs the full number with country code, e.g. 5511999999999')
    }
    jid = `${digits}@s.whatsapp.net`
  } else {
    jid = jidNormalizedUser(sock.user.id)
  }
  const result = await sock.sendMessage(jid, content)
  return { ok: true, id: (result && result.key && result.key.id) || null }
}

async function requestPairingCode(phone) {
  if (connected) throw new Error('this device is already linked')
  if (!sock) throw new Error('the bridge is still starting - try again in a few seconds')
  if (sock.authState && sock.authState.creds && sock.authState.creds.registered) {
    throw new Error('a session is already registered - press Unlink first')
  }
  const digits = String(phone).replace(/[^0-9]/g, '')
  if (digits.length < 10) throw new Error('enter the full number with country code, e.g. 5511999999999')
  const code = await sock.requestPairingCode(digits)
  pairingCode = code
  pairingCodeAt = Date.now()
  clearQr()
  log('pairing code requested')
  return code
}

// ----------------------------------------------------------------------- http
function json(response, status, payload) {
  const body = JSON.stringify(payload)
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  })
  response.end(body)
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    request.on('data', (chunk) => {
      size += chunk.length
      // Photos are forwarded as base64, so the ceiling is generous.
      if (size > 16 * 1024 * 1024) {
        reject(new Error('body too large (max 16 MB)'))
        request.destroy()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8')
      if (!raw) return resolve({})
      try {
        resolve(JSON.parse(raw))
      } catch (error) {
        reject(new Error('invalid JSON body'))
      }
    })
    request.on('error', reject)
  })
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`)

  if (request.method === 'GET' && url.pathname === '/health') {
    return json(response, 200, { ok: true, connected })
  }

  if (request.method === 'GET' && url.pathname === '/status') {
    return json(response, 200, publicState())
  }

  if (request.method === 'POST' && url.pathname === '/send') {
    try {
      const body = await readBody(request)
      const text = typeof body.text === 'string' ? body.text : ''
      // Photo cards travel as { image, mimetype, caption }; accept "text" there
      // too so older clients keep working.
      const caption =
        typeof body.caption === 'string' && body.caption.trim() ? body.caption : text
      const image = typeof body.image === 'string' ? body.image : ''
      if (!text.trim() && !caption.trim() && !image) {
        return json(response, 400, { ok: false, error: 'text or image is required' })
      }
      if (!connected) {
        return json(response, 409, {
          ok: false,
          error: lastError || 'not linked - scan the QR code',
        })
      }

      let content
      if (image) {
        const buffer = Buffer.from(image, 'base64')
        if (!buffer.length) {
          return json(response, 400, { ok: false, error: 'image is empty' })
        }
        content = { image: buffer, mimetype: body.mimetype || 'image/jpeg' }
        if (caption.trim()) content.caption = caption.slice(0, 1024)
      } else {
        content = { text }
      }

      const result = await sendMessage(content, body.to)
      log('sent message', result.id, image ? '(photo)' : '')
      return json(response, 200, result)
    } catch (error) {
      lastError = error.message
      log('send failed:', error.message)
      return json(response, 500, { ok: false, error: error.message })
    }
  }

  if (request.method === 'POST' && url.pathname === '/pair') {
    try {
      const body = await readBody(request)
      const code = await requestPairingCode(body.phone || '')
      return json(response, 200, { ok: true, code })
    } catch (error) {
      lastError = error.message
      log('pairing failed:', error.message)
      return json(response, 400, { ok: false, error: error.message })
    }
  }

  if (request.method === 'POST' && url.pathname === '/logout') {
    try {
      stopped = true
      if (sock) {
        try {
          await sock.logout()
        } catch (error) {
          log('logout call failed:', error.message)
        }
        try {
          sock.end(undefined)
        } catch (error) {
          /* already closed */
        }
      }
      sock = null
      connected = false
      me = null
      clearQr()
      clearPairingCode()
      lastError = 'unlinked'
      wipeAuth()
      stopped = false
      reconnectDelay = 500
      setTimeout(connect, 500)
      return json(response, 200, { ok: true, unlinked: true })
    } catch (error) {
      stopped = false
      return json(response, 500, { ok: false, error: error.message })
    }
  }

  return json(response, 404, { ok: false, error: 'not found' })
})

server.listen(PORT, HOST, () => {
  log(`bridge listening on http://${HOST}:${PORT} (auth: ${AUTH_DIR})`)
  connect()
})

process.on('unhandledRejection', (reason) => {
  lastError = String(reason)
  log('unhandled rejection:', reason)
})

process.on('uncaughtException', (error) => {
  lastError = `${error.name || 'Error'}: ${error.message}`
  log('uncaught exception:', error)
})

process.on('SIGTERM', () => {
  stopped = true
  log('stopping')
  if (sock) {
    try {
      sock.end(undefined)
    } catch (error) {
      /* ignore */
    }
  }
  server.close(() => process.exit(0))
  setTimeout(() => process.exit(0), 2000)
})
