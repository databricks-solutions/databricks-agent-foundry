import { useState, type ReactNode } from 'react';
import { Blocks, ChevronRight, Lock, Minus, Plus, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { useResourcesPanel } from '@/contexts/ResourcesPanelContext';
import { useIsMobile } from '@/hooks/use-mobile';
import { NewSkillSection } from '@/components/new-skill-form';
import { UserSkillActions } from '@/components/user-skill-actions';
import { useMcpTools, useResourcePicks } from '@/hooks/use-resource-picks';
import {
  buildPanel,
  canAdd,
  limitFor,
  ofKind,
  pickKey,
  withoutPick,
  withPick,
  type Identity,
  type Pick,
  type PickRow,
  type ResourceEntry,
  type ResourceKind,
  type ResourcesResponse,
  type UserSkill,
} from '@/lib/resource-picks';
import { autoSelectNewSkill, type AuthoredSkill } from '@/lib/skill-draft';
import { cn } from '@/lib/utils';

/**
 * Agent Bricks: the Unity Gateway resources panel (spec.ui.resources_sidebar).
 *
 * Shows the user's chapter first -- their picks, then the skill and MCP
 * catalogs to add from, kept apart -- then what the agent always uses
 * (read-only, collapsed while the user can pick).  Each toggle is saved at
 * once and applies from the next message.
 */

const IDENTITY_LABEL: Record<Identity, string> = {
  obo: 'As you',
  sp: 'As app',
};

const USER_REASON_TEXT: Record<
  NonNullable<ResourcesResponse['user_reason']>,
  string
> = {
  no_user_token: 'Sign in through Databricks to see your own catalog.',
  listing_failed:
    'Adding resources from your own catalog is currently not available in this app.',
};

const AGENT_HINT = 'Always available to the agent.';

const PICK_STATUS_TEXT: Record<PickRow['status'], string | null> = {
  selected: null,
  included: 'The agent already uses this.',
  unavailable: 'No longer available to you.',
};

export function ResourcesPanelToggle() {
  const { resourcesSidebarEnabled } = useAppConfig();
  const { open, toggle } = useResourcesPanel();
  if (!resourcesSidebarEnabled) {
    return null;
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="outline"
          className={cn('h-8 px-2', open && 'bg-accent')}
          aria-pressed={open}
          aria-label="Resources"
          onClick={toggle}
        >
          <Blocks className="h-4 w-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent>Resources</TooltipContent>
    </Tooltip>
  );
}

export function ResourcesPanel() {
  const { resourcesSidebarEnabled } = useAppConfig();
  const { open, setOpen } = useResourcesPanel();
  const isMobile = useIsMobile();
  if (!resourcesSidebarEnabled) {
    return null;
  }
  if (isMobile) {
    return (
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="flex flex-col gap-0 p-0">
          <SheetTitle className="px-4 pt-4">Resources</SheetTitle>
          <PanelBody />
        </SheetContent>
      </Sheet>
    );
  }
  if (!open) {
    return null;
  }
  return (
    <aside className="hidden h-svh w-80 shrink-0 flex-col overflow-hidden bg-sidebar text-sidebar-foreground md:flex">
      <div className="flex h-[60px] items-center justify-between px-4">
        <h4 className="font-medium text-[16px]">Resources</h4>
        <Button
          variant="tertiary"
          className="h-8 px-2"
          aria-label="Close resources"
          onClick={() => setOpen(false)}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      <PanelBody />
    </aside>
  );
}

function PanelBody() {
  const {
    resources,
    picks,
    userSkills,
    isLoading,
    error,
    setPicks,
    refreshResources,
    refreshUserSkills,
    refreshPicks,
  } = useResourcePicks(true);
  if (isLoading) {
    return <PanelSkeleton />;
  }
  if (error || !resources) {
    return (
      <p className="px-4 text-muted-foreground text-sm">
        Resources could not be loaded.
      </p>
    );
  }
  const model = buildPanel(resources, picks, userSkills);
  const onSkillCreated = async (skill: AuthoredSkill) => {
    await Promise.all([refreshResources(), refreshUserSkills()]);
    if (resources.authoring) {
      await setPicks(
        autoSelectNewSkill(picks, skill, resources.limits, resources.authoring.store),
      );
    }
  };
  const onUserSkillsChanged = async () => {
    await Promise.all([refreshUserSkills(), refreshPicks()]);
  };
  const pickable =
    resources.limits.max_servers !== null ||
    resources.limits.max_skills !== null;
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-4 pb-4">
      {pickable && (
        <PickSections
          resources={resources}
          picks={picks}
          picked={model.picked}
          available={model.available}
          onChange={setPicks}
          onSkillCreated={onSkillCreated}
          userSkills={new Map(userSkills.map((skill) => [skill.fqn, skill]))}
          onUserSkillsChanged={onUserSkillsChanged}
        />
      )}
      <AgentSection entries={model.agent} defaultOpen={!pickable} />
    </div>
  );
}

function AgentSection({
  entries,
  defaultOpen,
}: {
  entries: ResourceEntry[];
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  if (entries.length === 0) {
    return <Section title="Agent" hint={AGENT_HINT} isEmpty />;
  }
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <section className="flex flex-col gap-2">
        <CollapsibleTrigger className="flex items-start gap-1 text-left">
          <ChevronRight
            className={cn('mt-0.5 h-4 w-4 shrink-0 transition-transform', open && 'rotate-90')}
          />
          <SectionHeading title={`Agent (${entries.length})`} hint={AGENT_HINT} />
        </CollapsibleTrigger>
        <CollapsibleContent className="flex flex-col gap-2">
          {entries.map((entry) => (
            <ResourceRow key={pickKey(entry)} entry={entry} locked />
          ))}
        </CollapsibleContent>
      </section>
    </Collapsible>
  );
}

function PickSections({
  resources,
  picks,
  picked,
  available,
  onChange,
  onSkillCreated,
  userSkills,
  onUserSkillsChanged,
}: {
  resources: ResourcesResponse;
  picks: Pick[];
  picked: PickRow[];
  available: ResourceEntry[];
  onChange: (next: Pick[]) => void;
  onSkillCreated: (skill: AuthoredSkill) => Promise<void>;
  userSkills: ReadonlyMap<string, UserSkill>;
  onUserSkillsChanged: () => Promise<void>;
}) {
  const catalogProps = { resources, picks, onChange, userSkills, onUserSkillsChanged };
  return (
    <section className="flex flex-col gap-4">
      <ChapterHeading title="User" />
      {resources.user_reason && (
        <p className="text-muted-foreground text-xs">
          {USER_REASON_TEXT[resources.user_reason]}
        </p>
      )}
      <Section
        title="Your picks"
        hint="Sent with each of your messages."
        isEmpty={picked.length === 0}
      >
        {picked.map((row) => (
          <UserSkillActions
            key={pickKey(row.pick)}
            fqn={row.pick.fqn}
            skills={userSkills}
            authoring={resources.authoring}
            onChanged={onUserSkillsChanged}
          >
            {(skillActions) => (
              <PickedRow
                row={row}
                actions={skillActions}
                onRemove={() => onChange(withoutPick(picks, row.pick))}
              />
            )}
          </UserSkillActions>
        ))}
      </Section>
      <CatalogSection
        {...catalogProps}
        kind="skill"
        title="Skill catalog"
        entries={ofKind(available, 'skill')}
      >
        {resources.authoring && (
          <NewSkillSection
            authoring={resources.authoring}
            onCreated={onSkillCreated}
          />
        )}
      </CatalogSection>
      <CatalogSection
        {...catalogProps}
        kind="mcp_service"
        title="MCP catalog"
        entries={ofKind(available, 'mcp_service')}
      />
    </section>
  );
}

function CatalogSection({
  kind,
  title,
  entries,
  resources,
  picks,
  onChange,
  userSkills,
  onUserSkillsChanged,
  children,
}: {
  kind: ResourceKind;
  title: string;
  entries: ResourceEntry[];
  resources: ResourcesResponse;
  picks: Pick[];
  onChange: (next: Pick[]) => void;
  userSkills: ReadonlyMap<string, UserSkill>;
  onUserSkillsChanged: () => Promise<void>;
  children?: ReactNode;
}) {
  const limit = limitFor(kind, resources.limits);
  if (limit === null) {
    return null;
  }
  const added = picks.filter((pick) => pick.kind === kind).length;
  return (
    <section className="flex flex-col gap-2">
      <SectionHeading title={title} hint={`${added} of ${limit} added.`} />
      {children}
      {entries.length === 0 ? (
        <p className="text-muted-foreground text-xs">None</p>
      ) : (
        entries.map((entry) => (
          <UserSkillActions
            key={pickKey(entry)}
            fqn={entry.fqn}
            skills={userSkills}
            authoring={resources.authoring}
            onChanged={onUserSkillsChanged}
          >
            {(skillActions) => (
              <ResourceRow
                entry={entry}
                action={
                  <>
                    {skillActions}
                    <AddButton
                      disabled={!canAdd(entry.kind, picks, resources.limits)}
                      onAdd={() => onChange(withPick(picks, entry))}
                    />
                  </>
                }
              />
            )}
          </UserSkillActions>
        ))
      )}
    </section>
  );
}

function ChapterHeading({ title }: { title: string }) {
  return (
    <h4 className="font-semibold text-muted-foreground text-xs uppercase tracking-wide">
      {title}
    </h4>
  );
}

function Section({
  title,
  hint,
  isEmpty,
  children,
}: {
  title: string;
  hint?: string;
  isEmpty: boolean;
  children?: ReactNode;
}) {
  return (
    <section className="flex flex-col gap-2">
      <SectionHeading title={title} hint={hint} />
      {isEmpty ? (
        <p className="text-muted-foreground text-xs">None</p>
      ) : (
        children
      )}
    </section>
  );
}

function SectionHeading({ title, hint }: { title: string; hint?: string }) {
  return (
    <div>
      <h5 className="font-medium text-sm">{title}</h5>
      {hint && <p className="text-muted-foreground text-xs">{hint}</p>}
    </div>
  );
}

function ResourceRow({
  entry,
  locked,
  action,
  note,
}: {
  entry: ResourceEntry;
  locked?: boolean;
  action?: ReactNode;
  note?: string | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const hasTools = entry.kind === 'mcp_service' && entry.source === 'unity_gateway';
  return (
    <Collapsible open={expanded} onOpenChange={setExpanded}>
      <div className="flex items-start gap-2 rounded-md border border-border p-2">
        <CollapsibleTrigger
          disabled={!hasTools}
          aria-label={`Show tools of ${entry.display_name}`}
          className="mt-0.5 disabled:invisible"
        >
          <ChevronRight
            className={cn('h-4 w-4 transition-transform', expanded && 'rotate-90')}
          />
        </CollapsibleTrigger>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-sm" title={entry.fqn}>
              {entry.display_name}
            </span>
            {locked && <Lock className="h-3 w-3 shrink-0 text-muted-foreground" />}
          </div>
          {entry.description && (
            <p className="line-clamp-2 text-muted-foreground text-xs">
              {entry.description}
            </p>
          )}
          <div className="mt-1 flex flex-wrap gap-1">
            <Badge variant="outline">{entry.kind === 'skill' ? 'Skill' : 'MCP'}</Badge>
            <Badge variant="secondary">{IDENTITY_LABEL[entry.identity]}</Badge>
          </div>
          {note && <p className="mt-1 text-muted-foreground text-xs">{note}</p>}
        </div>
        {action}
      </div>
      <CollapsibleContent className="pl-8">
        <ToolList fqn={entry.fqn} />
      </CollapsibleContent>
    </Collapsible>
  );
}

function PickedRow({
  row,
  actions,
  onRemove,
}: {
  row: PickRow;
  actions?: ReactNode;
  onRemove: () => void;
}) {
  const remove = (
    <Button
      variant="tertiary"
      className="h-6 px-1"
      aria-label="Remove pick"
      title={row.status === 'selected' ? 'Move to catalog' : 'Remove pick'}
      onClick={onRemove}
    >
      <Minus className="h-4 w-4" />
    </Button>
  );
  if (row.entry === null) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-border border-dashed p-2">
        <div className="min-w-0 flex-1">
          <span className="truncate text-muted-foreground text-sm line-through">
            {row.pick.fqn}
          </span>
          <p className="text-muted-foreground text-xs">
            {PICK_STATUS_TEXT.unavailable}
          </p>
        </div>
        {remove}
      </div>
    );
  }
  return (
    <ResourceRow
      entry={row.entry}
      note={PICK_STATUS_TEXT[row.status]}
      action={
        <>
          {actions}
          {remove}
        </>
      }
    />
  );
}

function AddButton({
  disabled,
  onAdd,
}: {
  disabled: boolean;
  onAdd: () => void;
}) {
  return (
    <Button
      variant="tertiary"
      className="h-6 px-1"
      aria-label="Add pick"
      title={disabled ? 'Pick limit reached' : 'Add'}
      disabled={disabled}
      onClick={onAdd}
    >
      <Plus className="h-4 w-4" />
    </Button>
  );
}

function ToolList({ fqn }: { fqn: string }) {
  const { tools, error, isLoading } = useMcpTools(fqn);
  if (isLoading) {
    return <Skeleton className="my-1 h-4 w-32" />;
  }
  if (error) {
    return (
      <p className="py-1 text-muted-foreground text-xs">
        Tools could not be listed.
      </p>
    );
  }
  return (
    <ul className="py-1 text-xs">
      {tools.map((tool) => (
        <li key={tool.name} className="truncate" title={tool.description}>
          {tool.name}
        </li>
      ))}
    </ul>
  );
}

function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-2 px-4">
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
      <Skeleton className="h-12 w-full" />
    </div>
  );
}
