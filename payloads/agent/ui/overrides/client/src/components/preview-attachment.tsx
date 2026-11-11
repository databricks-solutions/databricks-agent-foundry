import type { Attachment } from '@chat-template/core';
import { Loader } from './elements/loader';
import { Button } from './ui/button';
import { FileText, X } from 'lucide-react';

// Agent Bricks file-upload override (file-upload-feature.md).
// Upstream rendered every non-image attachment as a generic square "File" box,
// which suited image-only uploads. The file-upload capability attaches
// extracted documents (xlsx/docx/pdf/csv/txt/md/json), so non-image
// attachments now render as a compact document chip (icon + filename) instead.
export const PreviewAttachment = ({
  attachment,
  isUploading = false,
  onRemove,
}: {
  attachment: Attachment;
  isUploading?: boolean;
  onRemove?: () => void;
}) => {
  const { name, url, contentType } = attachment;
  const isImage = contentType?.startsWith('image');

  return (
    <div
      data-testid="input-attachment-preview"
      className="group relative max-w-64 max-h-64 overflow-hidden rounded-lg border bg-muted"
    >
      {isImage ? (
        <img
          src={url}
          alt={name ?? 'An image attachment'}
          className="size-full object-cover"
        />
      ) : (
        <div className="flex max-w-48 items-center gap-2 px-3 py-2 text-muted-foreground">
          <FileText size={16} className="shrink-0" />
          <span className="truncate text-xs">{name || 'File'}</span>
        </div>
      )}

      {isUploading && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/50">
          <Loader size={16} />
        </div>
      )}

      {onRemove && !isUploading && (
        <Button
          onClick={onRemove}
          size="sm"
          variant="destructive"
          className="absolute top-0.5 right-0.5 size-4 rounded-full p-0 opacity-0 transition-opacity group-hover:opacity-100"
        >
          <X size={8} />
        </Button>
      )}

      {isImage && (
        <div className="absolute inset-x-0 bottom-0 truncate bg-linear-to-t from-black/80 to-transparent px-1 py-0.5 text-[10px] text-white">
          {name}
        </div>
      )}
    </div>
  );
};
