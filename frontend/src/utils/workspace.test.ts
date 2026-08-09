import { describe, expect, it } from 'vitest'
import { getWorkspaceStoragePath, getWorkspaceWorkingPath, parentPath } from './workspace'

describe('workspace path helpers', () => {
  it('preserves POSIX and Windows roots when finding a parent', () => {
    expect(parentPath('/train.py')).toBe('/')
    expect(parentPath('C:\\train.py')).toBe('C:\\')
    expect(parentPath('C:\\work\\train.py')).toBe('C:\\work')
  })

  it('keeps working and storage paths as distinct concepts', () => {
    const workspace = {
      workspace_kind: 'script' as const,
      script_path: '/project/train.py',
      script_name: 'train.py',
      run_root: '/project/_pyruns_/train',
      working_root: '/data',
      tasks_dir: '/project/_pyruns_/train/tasks',
      workspace_ready: true,
      settings: {},
      templates: [],
    }
    expect(getWorkspaceWorkingPath(workspace)).toBe('/data')
    expect(getWorkspaceStoragePath(workspace)).toBe('/project/_pyruns_/train')
  })
})
