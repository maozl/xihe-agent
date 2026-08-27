// Persistence for workspace entities + per-conversation bindings, backed by a
// plaintext JSON file at ~/.xihe-desktop/workspaces.json (read/written through
// the desktop IPC bridge in main — the renderer never touches the filesystem).
// The file is pretty-printed so it's easy to inspect or hand-edit.
//
// One file holds both slices:
//   { "workspaces": Workspace[], "convWorkspace": Record<convId, workspaceId> }
//
// The binding map is deliberately separate from ConvMeta because
// syncConversations rebuilds ConvMeta from the server on every
// select/connect/turn-complete; keeping bindings here means they survive those
// rebuilds by construction — there is nothing on ConvMeta to drop.

import { desktop, type WorkspaceStore } from './desktop'
import type { Workspace } from '../store'

export type { WorkspaceStore }

/** Load + defensively validate the store file. Missing/corrupt → empty. */
export async function loadWorkspaceStore(): Promise<WorkspaceStore> {
  const data = await desktop.workspaceLoad()
  const workspaces: Workspace[] = Array.isArray(data?.workspaces)
    ? data.workspaces.filter(
        (w): w is Workspace =>
          !!w &&
          typeof w.id === 'string' &&
          typeof w.name === 'string' &&
          typeof w.workdir === 'string'
      )
    : []
  const convWorkspace: Record<string, string> = {}
  if (data?.convWorkspace && typeof data.convWorkspace === 'object') {
    for (const [k, v] of Object.entries(data.convWorkspace)) {
      if (typeof k === 'string' && typeof v === 'string') convWorkspace[k] = v
    }
  }
  return { workspaces, convWorkspace }
}

/** Persist both slices (main writes atomically). Fire-and-forget at call sites:
 *  in-memory state is the UI's source of truth; the file is the persisted copy. */
export async function saveWorkspaceStore(data: WorkspaceStore): Promise<void> {
  await desktop.workspaceSave(data)
}
