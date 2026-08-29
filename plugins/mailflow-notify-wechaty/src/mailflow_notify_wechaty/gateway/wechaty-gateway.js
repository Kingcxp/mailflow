#!/usr/bin/env node
/**
 * WeChaty gateway bridge for MailFlow.
 *
 * Runs a WeChaty pad-protocol bot and exposes the two endpoints the
 * mailflow-notify-wechaty notifier expects, plus a QR endpoint for the
 * Bots-tab login flow:
 *
 *   GET  /health        -> 200 when the bot session is logged in
 *   POST /send          -> {"to": {"type": "contact"|"room", "name": ...},
 *                           "text": ...}  forwards to the WeChaty contact/room
 *   GET  /qr            -> {"qrcode": "<base64 png>"} during login,
 *                          {"status": "logged_in"} once a session exists
 *
 * Usage:
 *   WECHATY_TOKEN=<pad token> node wechaty-gateway.js [--port 8788]
 *
 * The pad-protocol token comes from your WeChaty puppet provider (e.g.
 * wechaty-puppet-padlocal). Without a token the gateway starts but never
 * completes login — the QR endpoint reports the error.
 *
 * This is a reference bridge: any service implementing the same three
 * endpoints works with MailFlow.
 */

const http = require("http");
const { WechatyBuilder } = require("wechaty");
const QRCode = require("qrcode");

// WeChaty internals reject promises outside our control (puppet network
// errors, plugin teardown). Never let an unhandled rejection crash or
// spam stderr: log it and keep the health/QR endpoints answering.
process.on("unhandledRejection", (reason) => {
  console.error(`[wechaty-gateway] unhandled rejection: ${reason && reason.stack || reason}`);
});

const PORT = parseInt(process.argv[2] || process.env.GATEWAY_PORT || "8788", 10);
const TOKEN = process.env.WECHATY_TOKEN || "";

let bot = null;
let startedAt = null;
let lastQr = "";
let lastQrStatus = "pending";
let loginError = "";

function startBot() {
  startedAt = Date.now();
  if (TOKEN) {
    // pad protocol (paid): requires a platform token
    bot = WechatyBuilder.build({
      name: "mailflow-gateway",
      puppet: "wechaty-puppet-padlocal",
      puppetOptions: { token: TOKEN },
    });
  } else {
    // fallback: web protocol (wechat4u). Tencent shut the web protocol
    // down, so login may fail — kept as an option since it still works
    // for some accounts. Ban risk: use a disposable account.
    bot = WechatyBuilder.build({
      name: "mailflow-gateway",
      puppet: "wechaty-puppet-wechat4u",
    });
  }

  bot.on("scan", async (qrcode, status) => {
    try {
      // wechaty gives the QR *text*; render it to a PNG base64 so the
      // TUI can display it directly (the user has no network route to
      // the gateway host)
      lastQr = await QRCode.toDataURL(qrcode, { margin: 1 });
      lastQr = lastQr.replace(/^data:image\/png;base64,/, "");
      lastQrStatus = "scanning";
      loginError = "";
    } catch (err) {
      lastQr = qrcode;
      lastQrStatus = "scanning";
      loginError = "";
    }
  });
  bot.on("login", (user) => {
    lastQr = "";
    lastQrStatus = "logged_in";
    loginError = `logged in as ${user.name()}`;
  });
  bot.on("logout", () => {
    lastQrStatus = "pending";
  });
  bot.on("message", async (msg) => {
    // chat command flow: forward text messages to the MailFlow bot
    // endpoint; the reply is sent back to the same chat
    const BOT_URL = process.env.MAILFLOW_BOT_URL || "";
    if (!BOT_URL) return;
    if (msg.self()) return;
    const text = msg.text();
    if (!text) return;
    const room = msg.room();
    const chatType = room ? "group" : "private";
    const chatId = room ? room.id : msg.talker().id;
    const sender = msg.talker().id;
    try {
      const res = await fetch(BOT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: text,
          sender: sender,
          chat_id: chatId,
          chat_type: chatType,
          provider: process.env.MAILFLOW_PROVIDER || "",
          instance_id: process.env.MAILFLOW_INSTANCE || "",
        }),
      });
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.reply) {
        const room = msg.room();
        if (room) await room.say(data.reply);
        else await msg.talker().say(data.reply);
      }
    } catch (err) {
      console.error("[wechaty-gateway] bot dispatch failed:", err && err.message || err);
    }
  });
  bot.on("error", (err) => {
    loginError = String(err && err.message || err);
    lastQrStatus = "error";
  });

  bot.start().catch((err) => {
    loginError = String(err && err.message || err);
    lastQrStatus = "error";
  });
}

function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk) => { data += chunk; });
    req.on("end", () => {
      try { resolve(JSON.parse(data || "{}")); }
      catch { resolve({}); }
    });
  });
}

async function findTarget(type, name) {
  if (!bot) return null;
  if (type === "room") {
    const room = await bot.Room.find({ topic: name });
    return room;
  }
  const contact = await bot.Contact.find({ name });
  return contact;
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const sendJson = (code, payload) => {
    res.writeHead(code, { "Content-Type": "application/json" });
    res.end(JSON.stringify(payload));
  };

  if (req.method === "GET" && url.pathname === "/health") {
    const loggedIn = lastQrStatus === "logged_in";
    return sendJson(200, { ok: true, logged_in: loggedIn, status: lastQrStatus, error: loginError || undefined });
  }

  if (req.method === "GET" && url.pathname === "/qr") {
    if (lastQrStatus === "logged_in") return sendJson(200, { status: "logged_in" });
    if (lastQrStatus === "error") return sendJson(200, { status: "error", error: loginError });
    if (lastQr) return sendJson(200, { status: "scanning", qrcode: lastQr });
    // no scan event yet: after a grace period this is a failure, not a
    // wait (the web protocol is discontinued and may never emit a QR)
    if (startedAt && Date.now() - startedAt > 60000) {
      return sendJson(200, { status: "error", error: loginError || "no QR within 60s" });
    }
    return sendJson(200, { status: "pending" });
  }

  if (req.method === "POST" && url.pathname === "/send") {
    if (lastQrStatus !== "logged_in" || !bot) {
      return sendJson(503, { ok: false, error: "not logged in" });
    }
    const body = await readBody(req);
    const target = body.to || {};
    const text = String(body.text || "");
    if (!text) return sendJson(400, { ok: false, error: "text required" });
    try {
      const recipient = await findTarget(target.type, target.name);
      if (!recipient) return sendJson(404, { ok: false, error: `no ${target.type} named ${target.name}` });
      await recipient.say(text);
      return sendJson(200, { ok: true });
    } catch (err) {
      return sendJson(500, { ok: false, error: String(err && err.message || err) });
    }
  }

  return sendJson(404, { ok: false, error: "not found" });
});

startBot();
server.listen(PORT, "127.0.0.1", () => {
  console.log(`wechaty gateway listening on http://127.0.0.1:${PORT}`);
});
