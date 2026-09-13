---
name: whatsapp-mac-chat-db
description: |
  Reading the WhatsApp Desktop message database on macOS — recent chats,
  unread counts, last message text, a conversation's history. Use for "my
  top chats", "what did X say", "unread messages", or any request for
  WhatsApp content on this Mac.
platforms: [macos]
---

## Pattern

DB: `~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite`
Core Data schema. **Copy it to the SANDBOX first and query the copy** —
the app holds it open, and a stray write would corrupt the user's chats.
Copy the `-wal` and `-shm` siblings too, or you read stale rows.

Tables and the columns that matter:

- `ZWACHATSESSION` — one row per chat: `Z_PK`, `ZPARTNERNAME` (display
  name), `ZCONTACTJID`, `ZUNREADCOUNT`, `ZARCHIVED`, `ZREMOVED`,
  `ZLASTMESSAGEDATE`, `ZLASTMESSAGETEXT`, `ZLASTMESSAGE` → FK to
  `ZWAMESSAGE.Z_PK`.
- `ZWAMESSAGE` — `Z_PK`, `ZTEXT`, `ZMESSAGEDATE`, `ZFROMJID`, `ZTOJID`,
  `ZCHATSESSION` (FK back to the session).

Dates are Core Data seconds since 2001-01-01: `unix = value + 978307200`.

Recent real chats:

```sql
SELECT s.ZPARTNERNAME, s.ZCONTACTJID, s.ZUNREADCOUNT,
       COALESCE(m.ZTEXT, s.ZLASTMESSAGETEXT) AS body,
       COALESCE(m.ZMESSAGEDATE, s.ZLASTMESSAGEDATE) AS ts
FROM ZWACHATSESSION s
LEFT JOIN ZWAMESSAGE m ON m.Z_PK = s.ZLASTMESSAGE
WHERE s.ZREMOVED = 0
  AND (s.ZCONTACTJID LIKE '%@s.whatsapp.net'   -- people
    OR s.ZCONTACTJID LIKE '%@g.us')            -- groups
ORDER BY ts DESC LIMIT 10;
```

## Gotchas

- **Status updates, channels and the official "WhatsApp" row are sessions
  too.** Ordering by `ZLASTMESSAGEDATE` without the JID filter above mixes
  them into "recent chats" and the user will (correctly) say the list is
  wrong. Only `@s.whatsapp.net` and `@g.us` are conversations.
- **Pinned chats carry a huge sentinel `ZLASTMESSAGEDATE`** (12+ digits,
  far past any real date). Never convert it to a timestamp — it prints as a
  nonsense future date. Take the date from the joined `ZWAMESSAGE`, or from
  `MAX(ZMESSAGEDATE) WHERE ZCHATSESSION = s.Z_PK`.
- `ZLASTMESSAGETEXT`/`ZTEXT` is NULL for media, calls and system events.
  Say "[media]" — do not silently render an empty message.
- Group chats have no per-sender name in the session row; the sender is on
  the message (`ZFROMJID`).
- Present the result as a Markdown table in the reply. This is private
  data: never copy it into a file or an artifact unless asked.
