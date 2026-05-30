import type { WorkspaceInfo } from '@/types'

export function parentPath(path?: string): string {
  const value = String(path || '').trim()
  if (!value) {
    return ''
  }
  const index = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'))
  if (index < 0) {
    return ''
  }
  if (index === 0) {
    return value.slice(0, 1)
  }
  if (index === 2 && /^[A-Za-z]:[\\/]/.test(value)) {
    return value.slice(0, 3)
  }
  return value.slice(0, index)
}

export function getWorkspaceWorkingPath(workspace?: WorkspaceInfo | null): string {
  if (!workspace) {
    return ''
  }
  if (workspace.working_root) {
    return workspace.working_root
  }
  if (workspace.workspace_kind === 'shell') {
    return workspace.project_root || workspace.run_root || ''
  }
  return parentPath(workspace.script_path) || workspace.run_root || ''
}

export function getWorkspaceStoragePath(workspace?: WorkspaceInfo | null): string {
  return workspace?.run_root || ''
}
