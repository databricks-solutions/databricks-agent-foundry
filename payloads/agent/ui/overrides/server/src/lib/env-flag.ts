/** Truthy parsing mirrors agent_foundry.env.read_bool ('true'/'1'/'yes'). */
export function envFlag(name: string): boolean {
  const raw = (process.env[name] ?? '').trim().toLowerCase();
  return raw === 'true' || raw === '1' || raw === 'yes';
}

/** A positive whole number from `name`, or `fallback` when unset or malformed. */
export function positiveIntFromEnv(name: string, fallback: number): number {
  const value = Number((process.env[name] ?? '').trim());
  return Number.isInteger(value) && value > 0 ? value : fallback;
}
