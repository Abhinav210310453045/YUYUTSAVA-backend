/**
 * A colour at an opacity, for any CSS colour — a hex literal or a theme variable.
 *
 * Appending a hex alpha (`${color}55`) only works when `color` is a hex literal.
 * Since the named themes, most accents are variables (`var(--text-info)`), and
 * `var(--text-info)55` is invalid CSS: the whole declaration is dropped, so those
 * borders, glows and tints silently disappeared. `color-mix` with `transparent`
 * gives the same result for hex colours and works for variables.
 */
export function alpha(color, percent) {
  return `color-mix(in srgb, ${color} ${percent}%, transparent)`
}
