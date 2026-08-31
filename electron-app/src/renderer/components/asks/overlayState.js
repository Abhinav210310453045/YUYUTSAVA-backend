// The overlay window is shared by the voice conversation and pending asks.
// They must not stack: a permission card sitting on top of the voice pill is
// unreadable, and the pill is meaningless while you're being asked something.
//
// This is the one bit of state they both need — deliberately a tiny module
// rather than context, because they are independent roots in the same window.

let askShowing = false
const listeners = new Set()

export function setAskShowing(next) {
  const v = !!next
  if (v === askShowing) return
  askShowing = v
  for (const fn of listeners) { try { fn(v) } catch { /* ignore */ } }
}

export function subscribeAskShowing(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function isAskShowing() { return askShowing }
