import { startE2EServer } from './start-server.mjs'

export default async function globalSetup() {
  return startE2EServer()
}
