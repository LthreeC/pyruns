import { expect, it } from 'vitest'
import { parseConfigYaml, parseIntegerInput, stringifyConfigYaml } from './configYaml'

it('rounds integer input exactly, including decimal ties and exponent notation', () => {
  const cases = [
    ['9007199254740993', 9007199254740993n],
    ['9007199254740993.5', 9007199254740994n],
    ['-9007199254740993.5', -9007199254740993n],
    ['9.007199254740993e15', 9007199254740993n],
    ['-.5', 0n],
    ['-1.5001', -2n],
    ['1e-999999', 0n],
    ['0e999999', 0n],
    ['0x20000000000001', 9007199254740993n],
  ] as const
  for (const [text, expected] of cases) expect(parseIntegerInput(text), text).toBe(expected)
  for (const text of ['', '.', '1 | 2', '1e999999']) expect(parseIntegerInput(text), text).toBeNull()
})

it('serializes special floats and numeric-looking strings without changing their types', () => {
  const values = [0n, 1n, 0, -0, 1, 1e-7, 1e21, Infinity, -Infinity, NaN, '1.0', '1e-7', 'true']
  expect(parseConfigYaml(stringifyConfigYaml(values))).toEqual(values)
})

it('rejects duplicate integer and float keys with the same numeric value', () => {
  expect(() => parseConfigYaml('1: first\n1.0: second\n')).toThrow('Map keys must be unique')
  expect(() => parseConfigYaml('1.0: first\n1: second\n')).toThrow('Map keys must be unique')
})
