// What a notice can offer the user, and the typed commands that do the same.
//
// The daemon names a notice's actions (`conversation/recovery.NOTICE_ACTIONS`)
// instead of spelling a command, because a command only means something in the
// terminal. Here each action is a button on the notice row; the ids below are
// pinned to the daemon's by test/daemon/web/test_notice_actions_ui_contract.py.
//
// The composer also takes the REPL's spelling: "/new" or "/retry" typed here is
// the action, not a message to the model — before this, "/new" was sent to the
// agent as text.

export const NOTICE_ACTION_IDS = Object.freeze(['retry', 'new_chat'])

const COMMANDS = Object.freeze({ '/retry': 'retry', '/new': 'new_chat' })

// The action a typed line stands for, or null when it is a message.
export const commandAction = (text) => COMMANDS[(text || '').trim().toLowerCase()] || null

// The actions a notice can still be acted on with: the known ones, and only
// while it is the newest row and nothing is running — once the conversation has
// moved on, "Retry" would retry something else.
export function liveActions(messages, index, busy) {
  const m = messages[index]
  if (busy || index !== messages.length - 1 || m?.role !== 'notice') return []
  return (m.actions || []).filter((a) => NOTICE_ACTION_IDS.includes(a?.id))
}
