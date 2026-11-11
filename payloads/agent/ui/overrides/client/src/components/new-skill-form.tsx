import { useState, type FormEvent, type ReactNode } from 'react';
import { ChevronRight } from 'lucide-react';
import { defaultRehypePlugins, Streamdown } from 'streamdown';
import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { toast } from '@/components/toast';
import { createSkill, updateSkill } from '@/hooks/use-resource-picks';
import type { UserSkill } from '@/lib/resource-picks';
import {
  AUTHORING_UNAVAILABLE_TEXT,
  MAX_DESCRIPTION_CHARS,
  MAX_SLUG_CHARS,
  savedInText,
  validateDraft,
  type AuthoredSkill,
  type AuthoringInfo,
  type DraftErrors,
  type SkillDraft,
} from '@/lib/skill-draft';
import { cn } from '@/lib/utils';

/**
 * Agent Bricks: the skill form of the resources panel
 * (`skills.gateway.authoring`, or Lakebase user skills).  A skill is created
 * as the signed-in user through `POST /api/agent/skills`, and a Lakebase
 * skill edited through `PUT /api/agent/skills/:fqn`; the server's refusal is
 * shown as is.
 *
 * The preview renders the user's own markdown without raw HTML (no
 * `rehype-raw`) and without images, so it cannot load or run anything.
 */

const EMPTY_DRAFT: SkillDraft = { slug: '', description: '', body: '' };
const PREVIEW_REHYPE_PLUGINS = [
  defaultRehypePlugins.sanitize,
  defaultRehypePlugins.harden,
];
const PREVIEW_COMPONENTS = { img: () => null };

type BodyTab = 'write' | 'preview';

function reportSaveError(error: unknown): null {
  const message = error instanceof Error ? error.message : String(error);
  toast({ type: 'error', description: `Could not save the skill: ${message}` });
  return null;
}

function reportRefreshError(): void {
  toast({
    type: 'error',
    description: 'The skill was saved; reopen the panel to see it.',
  });
}

function draftOf(skill: UserSkill): SkillDraft {
  return { slug: skill.display_name, description: skill.description, body: skill.body };
}

function saveSkill(draft: SkillDraft, existing?: UserSkill): Promise<AuthoredSkill> {
  return existing
    ? updateSkill(existing.fqn, { description: draft.description, body: draft.body })
    : createSkill(draft);
}

function submitLabel(editing: boolean, submitting: boolean): string {
  if (editing) {
    return submitting ? 'Saving…' : 'Save';
  }
  return submitting ? 'Creating…' : 'Create and pick';
}

export function NewSkillSection({
  authoring,
  onCreated,
}: {
  authoring: AuthoringInfo;
  onCreated: (skill: AuthoredSkill) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  if (authoring.unavailable_reason) {
    return (
      <div className="flex flex-col gap-1 opacity-60" aria-disabled="true">
        <span className="flex items-center gap-1 font-medium text-sm">
          <ChevronRight className="h-4 w-4" />
          New skill
        </span>
        <p className="text-muted-foreground text-xs">
          {AUTHORING_UNAVAILABLE_TEXT[authoring.unavailable_reason]}
        </p>
      </div>
    );
  }
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex items-center gap-1 font-medium text-sm">
        <ChevronRight
          className={cn('h-4 w-4 transition-transform', open && 'rotate-90')}
        />
        New skill
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-2">
        <SkillForm
          authoring={authoring}
          onSaved={async (skill) => {
            await onCreated(skill);
            setOpen(false);
          }}
        />
      </CollapsibleContent>
    </Collapsible>
  );
}

/** Creates a skill, or edits *existing* (its name is its id and stays). */
export function SkillForm({
  authoring,
  existing,
  onSaved,
}: {
  authoring: AuthoringInfo;
  existing?: UserSkill;
  onSaved: (skill: AuthoredSkill) => Promise<void>;
}) {
  const [draft, setDraft] = useState<SkillDraft>(
    existing ? draftOf(existing) : EMPTY_DRAFT,
  );
  const [showErrors, setShowErrors] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const errors: DraftErrors = showErrors ? validateDraft(draft, authoring) : {};
  const update = (field: keyof SkillDraft, value: string) =>
    setDraft((current) => ({ ...current, [field]: value }));

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (Object.keys(validateDraft(draft, authoring)).length > 0) {
      setShowErrors(true);
      return;
    }
    setSubmitting(true);
    const skill = await saveSkill(draft, existing).catch(reportSaveError);
    setSubmitting(false);
    if (!skill) {
      return;
    }
    if (!existing) {
      setDraft(EMPTY_DRAFT);
    }
    setShowErrors(false);
    const verb = existing ? 'Saved' : 'Created';
    toast({ type: 'success', description: `${verb} ${skill.display_name}.` });
    await onSaved(skill).catch(reportRefreshError);
  };

  return (
    <form className="flex flex-col gap-2" onSubmit={submit} noValidate>
      <p className="text-muted-foreground text-xs">
        {savedInText(authoring)}
      </p>
      <Field label="Name" error={errors.slug}>
        <Input
          value={draft.slug}
          maxLength={MAX_SLUG_CHARS}
          placeholder="ticket-triage"
          disabled={existing !== undefined}
          aria-label="Skill name"
          onChange={(event) => update('slug', event.target.value)}
        />
      </Field>
      <Field label="When to use it" error={errors.description}>
        <Input
          value={draft.description}
          maxLength={MAX_DESCRIPTION_CHARS}
          aria-label="Skill description"
          onChange={(event) => update('description', event.target.value)}
        />
      </Field>
      <Field label="Instructions" error={errors.body}>
        <BodyEditor
          body={draft.body}
          maxChars={authoring.max_skill_md_chars}
          onChange={(value) => update('body', value)}
        />
      </Field>
      <Button type="submit" className="self-end" disabled={submitting}>
        {submitLabel(existing !== undefined, submitting)}
      </Button>
    </form>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span className="font-medium text-xs">{label}</span>
      {children}
      {error && <p className="text-destructive text-xs">{error}</p>}
    </div>
  );
}

function BodyEditor({
  body,
  maxChars,
  onChange,
}: {
  body: string;
  maxChars: number;
  onChange: (value: string) => void;
}) {
  const [tab, setTab] = useState<BodyTab>('write');
  return (
    <div className="flex flex-col gap-1">
      <div className="flex gap-1" role="tablist">
        <TabButton tab="write" current={tab} onSelect={setTab} />
        <TabButton tab="preview" current={tab} onSelect={setTab} />
        <span className="ml-auto self-center text-muted-foreground text-xs">
          {body.length} / {maxChars}
        </span>
      </div>
      {tab === 'write' ? (
        <Textarea
          value={body}
          rows={8}
          aria-label="Skill instructions (markdown)"
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <div className="max-h-64 overflow-y-auto rounded-md border border-border p-2 text-sm">
          <Streamdown
            rehypePlugins={PREVIEW_REHYPE_PLUGINS}
            components={PREVIEW_COMPONENTS}
          >
            {body}
          </Streamdown>
        </div>
      )}
    </div>
  );
}

function TabButton({
  tab,
  current,
  onSelect,
}: {
  tab: BodyTab;
  current: BodyTab;
  onSelect: (tab: BodyTab) => void;
}) {
  return (
    <Button
      type="button"
      role="tab"
      variant="tertiary"
      aria-selected={tab === current}
      className={cn('h-6 px-2 text-xs capitalize', tab === current && 'bg-accent')}
      onClick={() => onSelect(tab)}
    >
      {tab}
    </Button>
  );
}
