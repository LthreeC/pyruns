import { isScalar, parse, stringify, type Scalar, type SchemaOptions, type ToStringOptions } from 'yaml'

// Keep YAML integers distinct from floats, including 1 and 1.0.
export function parseConfigYaml(text: string): unknown {
  const value: unknown = parse(text, {
    mapAsMap: true,
    intAsBigInt: true,
    uniqueKeys: (first, second) => {
      if (!isScalar(first) || !isScalar(second)) return first === second
      const a = first.value, b = second.value
      if (a === b) return true
      // bigint and integral floats still denote the same numeric mapping key.
      const integer = typeof a === 'bigint' ? a : b
      const floating = typeof a === 'bigint' ? b : a
      return typeof integer === 'bigint' && typeof floating === 'number'
        && Number.isInteger(floating) && integer === BigInt(floating)
    },
  })
  rejectRecursiveAliases(value)
  return value
}

function rejectRecursiveAliases(value: unknown): void {
  const ancestors = new Set<object>()
  const checked = new Set<object>()
  const visit = (current: unknown) => {
    if (!(current instanceof Map) && !Array.isArray(current) && !(current instanceof Set)) return
    if (ancestors.has(current)) throw new Error('Recursive YAML aliases are not supported.')
    if (checked.has(current)) return
    ancestors.add(current)
    if (current instanceof Map) {
      for (const [key, item] of current) {
        visit(key)
        visit(item)
      }
    } else {
      for (const item of current) visit(item)
    }
    ancestors.delete(current)
    checked.add(current)
  }
  visit(value)
}

function stringifyFloat({ value }: Scalar): string {
  const number = Number(value)
  if (!Number.isFinite(number)) return Number.isNaN(number) ? '.nan' : number < 0 ? '-.inf' : '.inf'
  const [mantissa, exponent] = (Object.is(number, -0) ? '-0' : String(number)).split('e')
  // The decimal point also makes exponent notation unambiguous to Python YAML loaders.
  return (mantissa.includes('.') ? mantissa : `${mantissa}.0`) + (exponent === undefined ? '' : `e${exponent}`)
}

const numericTags: SchemaOptions['customTags'] = tags => tags.map(tag => {
  if (typeof tag === 'string') return tag
  if (tag.tag === 'tag:yaml.org,2002:int') return { ...tag, identify: value => typeof value === 'bigint' }
  if (tag.tag === 'tag:yaml.org,2002:float' && !tag.collection) return { ...tag, stringify: stringifyFloat }
  return tag
})

export function stringifyConfigYaml(value: unknown, options?: ToStringOptions): string {
  return stringify(value, { ...options, customTags: numericTags })
}

export function parseIntegerInput(text: string): bigint | null {
  if (!text.trim()) return null
  try {
    return BigInt(text)
  } catch {
    // Decimal and exponent input retain the form's rounding toward +infinity at ties.
  }
  const parts = /^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(text)
  if (!parts || !(parts[2] || parts[3]) || !Number.isFinite(Number(text))) return null
  const fraction = parts[3] || ''
  const digits = (parts[2] + fraction).replace(/^0+/, '') || '0'
  const magnitude = BigInt(digits)
  if (magnitude === 0n) return 0n
  const shift = Number(parts[4] || 0) - fraction.length
  const negative = parts[1] === '-'
  let rounded: bigint
  if (shift >= 0) {
    rounded = magnitude * 10n ** BigInt(shift)
  } else if (-shift > digits.length) {
    return 0n
  } else {
    const divisor = 10n ** BigInt(-shift)
    rounded = magnitude / divisor
    const twiceRemainder = (magnitude % divisor) * 2n
    if (negative ? twiceRemainder > divisor : twiceRemainder >= divisor) rounded += 1n
  }
  return negative ? -rounded : rounded
}
