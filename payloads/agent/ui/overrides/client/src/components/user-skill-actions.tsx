import { useState, type ReactNode } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Button } from '@/components/ui/button';
import { toast } from '@/components/toast';
import { SkillForm } from '@/components/new-skill-form';
import { deleteSkill } from '@/hooks/use-resource-picks';
import type { UserSkill } from '@/lib/resource-picks';
import type { AuthoringInfo } from '@/lib/skill-draft';

/**
 * Agent Bricks: edit and delete for the user's Lakebase skills in the
 * resources panel.  Wraps a row: *children* receives the extra buttons (or
 * `null` when *fqn* is not one of the user's skills) and renders the row;
 * the edit form opens below it.
 */

async function removeSkill(
  skill: UserSkill,
  onChanged: () => Promise<void>,
): Promise<void> {
  try {
    await deleteSkill(skill.fqn);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    toast({ type: 'error', description: `Could not delete the skill: ${message}` });
    return;
  }
  toast({ type: 'success', description: `Deleted ${skill.display_name}.` });
  await onChanged();
}

export function UserSkillActions({
  fqn,
  skills,
  authoring,
  onChanged,
  children,
}: {
  fqn: string;
  skills: ReadonlyMap<string, UserSkill>;
  authoring: AuthoringInfo | null;
  onChanged: () => Promise<void>;
  children: (actions: ReactNode) => ReactNode;
}) {
  const skill = skills.get(fqn);
  if (!skill || authoring?.store !== 'lakebase') {
    return <>{children(null)}</>;
  }
  return (
    <EditableSkill skill={skill} authoring={authoring} onChanged={onChanged}>
      {children}
    </EditableSkill>
  );
}

function EditableSkill({
  skill,
  authoring,
  onChanged,
  children,
}: {
  skill: UserSkill;
  authoring: AuthoringInfo;
  onChanged: () => Promise<void>;
  children: (actions: ReactNode) => ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const actions = (
    <>
      <IconButton
        label={`Edit ${skill.display_name}`}
        pressed={editing}
        onClick={() => setEditing((open) => !open)}
      >
        <Pencil className="h-4 w-4" />
      </IconButton>
      <IconButton
        label={`Delete ${skill.display_name}`}
        onClick={() => setConfirming(true)}
      >
        <Trash2 className="h-4 w-4" />
      </IconButton>
    </>
  );
  return (
    <div className="flex flex-col gap-2">
      {children(actions)}
      {editing && (
        <div className="rounded-md border border-border p-2">
          <SkillForm
            authoring={authoring}
            existing={skill}
            onSaved={async () => {
              await onChanged();
              setEditing(false);
            }}
          />
        </div>
      )}
      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {skill.display_name}?</AlertDialogTitle>
            <AlertDialogDescription>
              The skill is removed from your account and your picks. This
              cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={() => removeSkill(skill, onChanged)}>
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function IconButton({
  label,
  pressed,
  onClick,
  children,
}: {
  label: string;
  pressed?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <Button
      variant="tertiary"
      className="h-6 px-1"
      aria-label={label}
      aria-pressed={pressed}
      title={label}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}
