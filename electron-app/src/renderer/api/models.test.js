import { describe, expect, it } from 'vitest'
import { modelChoices } from './models'
import { aliasFor } from '../components/models/aliasFor'

describe('the models a picker offers', () => {
  it('lists the names, then the default and task models, once each', () => {
    const status = {
      configured: true,
      default: 'flash',
      models: [{ name: 'flash' }, { name: 'lite' }],
      tasks: [{ reference: 'flash' }, { reference: 'work/gemini-3.5-pro' }],
    }
    expect(modelChoices(status)).toEqual(['flash', 'lite', 'work/gemini-3.5-pro'])
  })

  it('offers nothing before models.toml exists', () => {
    expect(modelChoices({ configured: false })).toEqual([])
  })
})

describe('a name for an imported model', () => {
  it('is the id, with what a name cannot hold replaced', () => {
    expect(aliasFor('gemini-3.5-flash')).toBe('gemini-3.5-flash')
    expect(aliasFor('openai/gpt-oss-120b-maas')).toBe('openai-gpt-oss-120b-maas')
    expect(aliasFor('qwen3:8b')).toBe('qwen3-8b')
  })
})
