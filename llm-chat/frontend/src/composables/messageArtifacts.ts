import type { FileArtifact, Message } from '../types'

/** A file belongs to a message, not to whichever message is currently last. */
export function artifactKey(artifact: FileArtifact): string {
  return artifact.id && artifact.id > 0
    ? `artifact:${artifact.id}`
    : `artifact:${artifact.message_id || ''}:${artifact.path || artifact.name}`
}

export function upsertArtifact(artifacts: FileArtifact[], incoming: FileArtifact): void {
  const index = artifacts.findIndex(existing => {
    if (existing.id && incoming.id) return existing.id === incoming.id
    return (existing.message_id || '') === (incoming.message_id || '')
      && (existing.path || existing.name) === (incoming.path || incoming.name)
  })
  if (index < 0) artifacts.push({ ...incoming })
  else artifacts[index] = { ...artifacts[index], ...incoming }
}

export function bindMessageIdentity(message: Message, messageId?: string): void {
  if (!messageId || (message.message_id && message.message_id !== messageId)) return
  message.message_id = messageId
  for (const artifact of message.artifacts || []) {
    if (!artifact.message_id) artifact.message_id = messageId
  }
}

/** Explicit event ownership wins; old events can never migrate into a new turn. */
export function attachMessageArtifact(messages: Message[], current: Message, incoming: FileArtifact): boolean {
  if (incoming.source === 'uploaded') return false
  let owner = current
  if (incoming.message_id) {
    const matched = messages.find(message => message.role === 'assistant' && message.message_id === incoming.message_id)
    if (matched) owner = matched
    else if (!current.message_id) bindMessageIdentity(current, incoming.message_id)
    else return false
  }
  const artifact = { ...incoming, message_id: incoming.message_id || owner.message_id }
  owner.artifacts ||= []
  upsertArtifact(owner.artifacts, artifact)
  return true
}
