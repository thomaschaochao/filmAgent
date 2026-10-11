/** Local-only shot edits. No providers, persistence, or DOM access. */
import { validateProject } from './project-store.mjs';
import { LIMITS } from './workflows.mjs';

function shotIndex(project, shotId) {
  const index = project.shots.findIndex(shot => shot.id === shotId);
  if (index < 0) throw new Error('此镜头已不存在，请重新选择。');
  return index;
}

function renumber(project) {
  project.shots.forEach((shot, index) => { shot.number = index + 1; });
  return validateProject(project);
}

/**
 * Move by stable ID and renumber all shots in their resulting display order.
 * Sequence and all other fields remain unchanged. A boundary move returns an
 * unchanged, detached validated copy, including its existing shot numbers.
 */
export function moveShot(project, shotId, direction) {
  const next = validateProject(project);
  if (direction !== -1 && direction !== 1) throw new Error('镜头移动方向必须是 -1 或 1。');
  const index = shotIndex(next, shotId);
  const target = index + direction;
  if (target < 0 || target >= next.shots.length) return validateProject(next);
  [next.shots[index], next.shots[target]] = [next.shots[target], next.shots[index]];
  return renumber(next);
}

/** Duplicate immediately after the original with an independent ID and fields. */
export function duplicateShot(project, shotId) {
  const next = validateProject(project);
  const index = shotIndex(next, shotId);
  if (next.shots.length >= LIMITS.shots) throw new Error(`镜头数量已达到当前工作台 ${LIMITS.shots} 个上限。`);
  const copy = structuredClone(next.shots[index]);
  copy.id = globalThis.crypto.randomUUID();
  next.shots.splice(index + 1, 0, copy);
  return renumber(next);
}
