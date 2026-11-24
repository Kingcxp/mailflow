package main

import "testing"

func TestParseQuotedMessageIncludesSenderAndContent(t *testing.T) {
	text, quote, sender := parseQuotedMessage(`<msg><appmsg><title>请看这个文件</title><refermsg><svrid>123</svrid><fromusr>@mailflow</fromusr><content>已收到附件：agenda.pdf</content></refermsg></appmsg></msg>`)
	if text != "请看这个文件" || sender != "@mailflow" {
		t.Fatalf("unexpected quote metadata: text=%q sender=%q", text, sender)
	}
	if quote["message_id"] != "123" || quote["text"] != "已收到附件：agenda.pdf" {
		t.Fatalf("unexpected quote content: %#v", quote)
	}
}
