import { useCallback } from 'react';
import useSWR from 'swr';
import { toast } from '@/components/toast';
import type { Pick, ResourcesResponse, UserSkill } from '@/lib/resource-picks';
import type { AuthoredSkill, SkillDraft, SkillEdit } from '@/lib/skill-draft';

const RESOURCES_URL = '/api/agent/resources';
const PICKS_URL = '/api/preferences/resources';
const TOOLS_URL = '/api/agent/resources/tools';
const SKILLS_URL = '/api/agent/skills';
const USER_SKILLS_URL = '/api/user-skills';

interface PicksResponse {
  resources: Pick[];
}

interface UserSkillsResponse {
  skills: UserSkill[];
}

export interface McpTool {
  name: string;
  description: string;
}

interface ToolsResponse {
  fqn: string;
  tools: McpTool[];
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

async function savePicks(resources: Pick[]): Promise<PicksResponse> {
  const response = await fetch(PICKS_URL, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ resources }),
  });
  if (!response.ok) {
    throw new Error(`Saving picks failed (${response.status})`);
  }
  return response.json() as Promise<PicksResponse>;
}

async function errorDetail(response: Response): Promise<string> {
  const body = (await response.json().catch(() => null)) as {
    detail?: unknown;
  } | null;
  return typeof body?.detail === 'string'
    ? body.detail
    : `Request failed (${response.status})`;
}

/** Sends one skill write; throws with the server's reason. */
async function writeSkill(
  method: 'POST' | 'PUT' | 'DELETE',
  url: string,
  body?: SkillDraft | SkillEdit,
): Promise<Response> {
  const response = await fetch(url, {
    method,
    credentials: 'same-origin',
    ...(body && {
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    throw new Error(await errorDetail(response));
  }
  return response;
}

function skillUrl(fqn: string): string {
  return `${SKILLS_URL}/${encodeURIComponent(fqn)}`;
}

/** Creates the skill as the signed-in user; throws with the agent's reason. */
export async function createSkill(draft: SkillDraft): Promise<AuthoredSkill> {
  const response = await writeSkill('POST', SKILLS_URL, draft);
  return response.json() as Promise<AuthoredSkill>;
}

/** Edits one of the user's Lakebase skills. */
export async function updateSkill(
  fqn: string,
  edit: SkillEdit,
): Promise<AuthoredSkill> {
  const response = await writeSkill('PUT', skillUrl(fqn), edit);
  return response.json() as Promise<AuthoredSkill>;
}

/** Deletes one of the user's Lakebase skills; the server also drops its pick. */
export async function deleteSkill(fqn: string): Promise<void> {
  await writeSkill('DELETE', skillUrl(fqn));
}

/** Reloads the panel's skills and picks, e.g. after the agent saved a skill. */
export async function refreshUserSkillData(
  mutate: (key: string) => Promise<unknown>,
): Promise<void> {
  await Promise.all([mutate(USER_SKILLS_URL), mutate(PICKS_URL)]);
}

const SWR_OPTIONS = { revalidateOnFocus: false, revalidateOnReconnect: false };

/** The panel's data; requests start only when *enabled*. */
export function useResourcePicks(enabled: boolean) {
  const resources = useSWR<ResourcesResponse>(
    enabled ? RESOURCES_URL : null,
    getJson,
    SWR_OPTIONS,
  );
  const picks = useSWR<PicksResponse>(
    enabled ? PICKS_URL : null,
    getJson,
    SWR_OPTIONS,
  );
  const lakebase = resources.data?.authoring?.store === 'lakebase';
  const userSkills = useSWR<UserSkillsResponse>(
    enabled && lakebase ? USER_SKILLS_URL : null,
    getJson,
    SWR_OPTIONS,
  );
  const { mutate } = picks;

  const setPicks = useCallback(
    async (next: Pick[]) => {
      try {
        await mutate(savePicks(next), {
          optimisticData: { resources: next },
          rollbackOnError: true,
          revalidate: false,
        });
      } catch {
        toast({ type: 'error', description: 'Could not save your picks.' });
      }
    },
    [mutate],
  );

  return {
    resources: resources.data,
    picks: picks.data?.resources ?? [],
    userSkills: userSkills.data?.skills ?? [],
    isLoading: resources.isLoading || picks.isLoading || userSkills.isLoading,
    error: resources.error ?? picks.error ?? userSkills.error,
    setPicks,
    refreshResources: resources.mutate,
    refreshUserSkills: userSkills.mutate,
    refreshPicks: picks.mutate,
  };
}

/** One MCP service's tools, fetched only while *fqn* is set. */
export function useMcpTools(fqn: string | null) {
  const url = fqn ? `${TOOLS_URL}?fqn=${encodeURIComponent(fqn)}` : null;
  const { data, error, isLoading } = useSWR<ToolsResponse>(
    url,
    getJson,
    SWR_OPTIONS,
  );
  return { tools: data?.tools ?? [], error, isLoading };
}
