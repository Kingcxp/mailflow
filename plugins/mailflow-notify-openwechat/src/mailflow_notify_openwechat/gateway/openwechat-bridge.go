// openwechat-bridge: WeChat gateway bridge for MailFlow based on the
// openwechat Go SDK (https://github.com/eatmoreapple/openwechat).
//
// Exposes the MailFlow gateway HTTP contract:
//   GET  /health -> {"logged_in": bool, "status": "pending"|"scanning"|"logged_in"|"error", "error": ...}
//   GET  /qr     -> {"status": ..., "qrcode": "<base64 png>", "error": ...}
//   POST /send   -> {"to": {"type": "contact"|"room", "name": ...}, "text": ...}
//
// The QR is rendered to a PNG so the TUI can display it directly.
// Login is scan-to-login: no platform token required.
//
// Build:  go build -o openwechat-bridge openwechat-bridge.go
// Run:    GATEWAY_PORT=8789 ./openwechat-bridge

package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sync"
	"time"

	"github.com/eatmoreapple/openwechat"
	"github.com/skip2/go-qrcode"
)

type state struct {
	mu       sync.RWMutex
	lastQR   string
	status   string // pending | scanning | logged_in | error
	errMsg   string
	started  time.Time
	qrAsked  bool
}

var st = &state{status: "pending", started: time.Now()}

func main() {
	port := os.Getenv("GATEWAY_PORT")
	if port == "" {
		port = "8789"
	}
	bot := openwechat.DefaultBot(openwechat.Desktop)
	bot.UUIDCallback = func(uuid string) {
		q, err := qrcode.New("https://login.weixin.qq.com/l/"+uuid, qrcode.Medium)
		if err == nil {
			png, _ := q.PNG(256)
			st.mu.Lock()
			st.lastQR = base64.StdEncoding.EncodeToString(png)
			st.status = "scanning"
			st.errMsg = ""
			st.mu.Unlock()
		}
	}

	go func() {
		// wait until the TUI has asked for the QR at least once, so the
		// login flow starts when the user is ready to scan
		for {
			st.mu.RLock()
			asked := st.qrAsked
			st.mu.RUnlock()
			if asked {
				break
			}
			time.Sleep(300 * time.Millisecond)
		}
		storage := openwechat.NewFileHotReloadStorage("openwechat-session.json")
		defer storage.Close()
		// PushLogin: first run scans the QR (UUID callback), later runs
		// reuse the stored session (hot login, no re-scan)
		if err := bot.PushLogin(storage); err != nil {
			st.mu.Lock()
			st.status = "error"
			st.errMsg = fmt.Sprintf("login failed: %v", err)
			st.mu.Unlock()
			return
		}
		user, err := bot.GetCurrentUser()
		name := "wechat"
		if err == nil && user != nil {
			name = user.NickName
		}
		st.mu.Lock()
		st.status = "logged_in"
		st.errMsg = "logged in as " + name
		st.mu.Unlock()
	}()

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"logged_in": snapshot().status == "logged_in",
			"status":    snapshot().status,
			"error":     snapshot().errMsg,
		})
	})
	http.HandleFunc("/qr", func(w http.ResponseWriter, r *http.Request) {
		st.mu.Lock()
		st.qrAsked = true
		s := state{status: st.status, lastQR: st.lastQR, errMsg: st.errMsg, started: st.started}
		st.mu.Unlock()
		switch s.status {
		case "logged_in":
			writeJSON(w, http.StatusOK, map[string]any{"status": "logged_in"})
		case "error":
			writeJSON(w, http.StatusOK, map[string]any{"status": "error", "error": s.errMsg})
		case "scanning":
			writeJSON(w, http.StatusOK, map[string]any{"status": "scanning", "qrcode": s.lastQR})
		default:
			if time.Since(s.started) > 60*time.Second {
				writeJSON(w, http.StatusOK, map[string]any{"status": "error", "error": s.errMsg + " (no QR within 60s)"})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"status": "pending"})
		}
	})
	http.HandleFunc("/send", func(w http.ResponseWriter, r *http.Request) {
		s := snapshot()
		if s.status != "logged_in" {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{"ok": false, "error": "not logged in"})
			return
		}
		var body struct {
			To struct {
				Type string `json:"type"`
				Name string `json:"name"`
			} `json:"to"`
			Text string `json:"text"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "bad json"})
			return
		}
		if body.Text == "" {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": "text required"})
			return
		}
		self, err := bot.GetCurrentUser()
		if err != nil || self == nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": "no session"})
			return
		}
		var sendErr error
		if body.To.Type == "room" {
			groups, _ := self.Groups()
			var target *openwechat.Group
			for _, g := range groups {
				if g.NickName == body.To.Name {
					target = g
					break
				}
			}
			if target == nil {
				writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "no room named " + body.To.Name})
				return
			}
			_, sendErr = target.SendText(body.Text)
		} else {
			contacts, _ := self.Friends()
			var target *openwechat.Friend
			for _, c := range contacts {
				if c.NickName == body.To.Name {
					target = c
					break
				}
			}
			if target == nil {
				writeJSON(w, http.StatusNotFound, map[string]any{"ok": false, "error": "no contact named " + body.To.Name})
				return
			}
			_, sendErr = target.SendText(body.Text)
		}
		if sendErr != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"ok": false, "error": sendErr.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})

	log.Printf("openwechat bridge listening on 127.0.0.1:%s", port)
	if err := http.ListenAndServe("127.0.0.1:"+port, nil); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

func snapshot() state {
	st.mu.RLock()
	defer st.mu.RUnlock()
	return state{lastQR: st.lastQR, status: st.status, errMsg: st.errMsg, started: st.started}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("write failed: %v", err)
	}
}
