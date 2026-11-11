/**
 * Agent Bricks: carries the user's sidebar picks from the chat route to
 * the agent request as `custom_inputs.selected_resources`, the picked
 * Lakebase user skills as `custom_inputs.user_skills`, and the stored
 * reasoning replay entries as `custom_inputs.reasoning_replay` (#681).
 *
 * The chat route sets the headers on the in-process request only;
 * `databricksFetch` removes them before anything goes over the network.
 * Header values must be Latin-1, so the JSON is percent-encoded.
 */
export const CONTEXT_HEADER_SELECTED_RESOURCES =
  'x-agent-foundry-selected-resources';
export const CONTEXT_HEADER_USER_SKILLS = 'x-agent-foundry-user-skills';
export const CONTEXT_HEADER_REASONING_REPLAY = 'x-agent-foundry-reasoning-replay';

const SELECTED_RESOURCES_INPUT = 'selected_resources';
const USER_SKILLS_INPUT = 'user_skills';
const REASONING_REPLAY_INPUT = 'reasoning_replay';

/** *value* as a context header value. */
export function encodeHeaderJson(value: unknown): string {
  return encodeURIComponent(JSON.stringify(value));
}

/** *body* with `custom_inputs[key]` set to the decoded *header*; unchanged when either is not JSON. */
export function withCustomInput(
  body: string,
  key: string,
  header: string,
): string {
  try {
    const request = JSON.parse(body);
    const value: unknown = JSON.parse(decodeURIComponent(header));
    return JSON.stringify({
      ...request,
      custom_inputs: { ...request.custom_inputs, [key]: value },
    });
  } catch {
    return body;
  }
}

/** *body* with the *header* picks set; unchanged when either is not JSON. */
export function withSelectedResources(body: string, header: string): string {
  return withCustomInput(body, SELECTED_RESOURCES_INPUT, header);
}

/** *body* with the *header* user skills set; unchanged when either is not JSON. */
export function withUserSkills(body: string, header: string): string {
  return withCustomInput(body, USER_SKILLS_INPUT, header);
}

/** *body* with the *header* reasoning replay entries set; unchanged when either is not JSON. */
export function withReasoningReplay(body: string, header: string): string {
  return withCustomInput(body, REASONING_REPLAY_INPUT, header);
}
