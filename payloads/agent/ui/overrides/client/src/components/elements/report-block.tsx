/**
 * Report card for the ``markdown-report`` fence handler in ``response.tsx``:
 * the rendered report under a header with the filename, the caller's
 * actions, a Markdown button that downloads the raw Markdown and an HTML button that
 * downloads the rendered report as a standalone page (``report-snapshot.ts``).
 */
import { type ReactNode, useCallback, useRef } from 'react';
import { Download, FileText } from 'lucide-react';

import { BlockCard, CardButton } from './block-card';
import { downloadTextFile } from './message-context';
import { reportHtml } from './report-snapshot';

const MARKDOWN_CONTENT_TYPE = 'text/markdown;charset=utf-8';
const HTML_CONTENT_TYPE = 'text/html;charset=utf-8';

interface ReportBlockProps {
  filename: string;
  markdown: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function ReportBlock({
  filename,
  markdown,
  actions,
  children,
}: ReportBlockProps) {
  const cardRef = useRef<HTMLDivElement>(null);
  const stem = filename.replace(/\.md$/, '');

  const handleDownload = useCallback(() => {
    downloadTextFile(filename, markdown, MARKDOWN_CONTENT_TYPE);
  }, [filename, markdown]);

  const handleHtmlDownload = useCallback(() => {
    if (cardRef.current) {
      downloadTextFile(
        `${stem}.html`,
        reportHtml(cardRef.current, stem),
        HTML_CONTENT_TYPE,
      );
    }
  }, [stem]);

  return (
    <BlockCard
      ref={cardRef}
      icon={FileText}
      label={filename}
      actions={
        <>
          {actions}
          <CardButton
            icon={Download}
            label="Markdown"
            ariaLabel={`Download ${filename}`}
            onClick={handleDownload}
          />
          <CardButton
            icon={Download}
            label="HTML"
            ariaLabel={`Download ${stem}.html`}
            onClick={handleHtmlDownload}
          />
        </>
      }
    >
      {children}
    </BlockCard>
  );
}

export default ReportBlock;
