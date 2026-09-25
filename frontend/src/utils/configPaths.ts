export type ConfigKey = string | number | bigint | boolean
export type ConfigPath = readonly ConfigKey[]
export type ConfigMap = Map<ConfigKey, unknown>

export function isConfigMap(value: unknown): value is ConfigMap {
  return value instanceof Map
}

// Include key types: YAML permits both 1 and "1" in the same mapping.
export function configPathId(path: ConfigPath): string {
  // Keep v2 numeric pins compatible when YAML integers are represented as bigint.
  return JSON.stringify(path.map(key => [typeof key === 'bigint' ? 'number' : typeof key, String(key)]))
}

export function configPathFromId(id: string): ConfigKey[] | null {
  try {
    const parts: unknown = JSON.parse(id)
    if (!Array.isArray(parts)) return null
    const path: ConfigKey[] = []
    for (const part of parts) {
      if (!Array.isArray(part) || part.length !== 2 || typeof part[1] !== 'string') return null
      const [type, value] = part
      if (type === 'string') path.push(value)
      else if (type === 'number') path.push(/^-?\d+$/.test(value) ? BigInt(value) : Number(value))
      else if (type === 'boolean' && (value === 'true' || value === 'false')) path.push(value === 'true')
      else return null
    }
    return configPathId(path) === id ? path : null
  } catch {
    return null
  }
}

export function formatConfigPath(path: ConfigPath): string {
  return path.map((key, index) => (
    typeof key === 'string' && /^[\p{L}_$][\p{L}\p{N}_$]*$/u.test(key)
      ? `${index ? '.' : ''}${key}`
      : `[${typeof key === 'string' ? JSON.stringify(key) : String(key)}]`
  )).join('')
}

export function getConfigValue(data: ConfigMap, path: ConfigPath): unknown {
  let current: unknown = data
  for (const key of path) {
    if (!isConfigMap(current)) return undefined
    current = current.get(resolveConfigKey(current, key))
  }
  return current
}

export function updateConfigValue(data: ConfigMap, path: ConfigPath, value: unknown): ConfigMap {
  if (!path.length) return data
  const [head, ...tail] = path
  const key = resolveConfigKey(data, head)
  if (!data.has(key)) return data
  if (!tail.length) return new Map(data).set(key, value)
  const child = data.get(key)
  if (!isConfigMap(child)) return data
  return new Map(data).set(key, updateConfigValue(child, tail, value))
}

function resolveConfigKey(data: ConfigMap, key: ConfigKey): ConfigKey {
  // A persisted [1] path may refer to an integral float key (1.0).
  if (typeof key === 'bigint' && !data.has(key)) {
    const number = Number(key)
    if (Number.isFinite(number) && BigInt(number) === key && data.has(number)) return number
  }
  return key
}
