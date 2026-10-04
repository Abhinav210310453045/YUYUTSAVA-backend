// A [models] name for a model id: the id itself, with what a name cannot hold replaced —
// the same rule as the CLI wizard's, so both name an imported model alike.
export function aliasFor(modelId) {
  const name = modelId.replace(/[^A-Za-z0-9_.-]/g, '-').replace(/^[-._]+|[-._]+$/g, '') || 'model'
  return /^[A-Za-z0-9]/.test(name) ? name : `m-${name}`
}
