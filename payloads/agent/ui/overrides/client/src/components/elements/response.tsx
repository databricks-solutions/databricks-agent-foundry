/**
 * Override of e2e-chatbot-app-next/client/src/components/elements/response.tsx.
 *
 * Adds a fence-dispatch hook on top of the upstream Streamdown wrapper:
 * features (#122 SQL rendering, #105 chart rendering, Mermaid diagrams,
 * SVG images, Markdown reports) register handlers
 * keyed by markdown fence language tag via `registerFenceRenderer`. A fence
 * whose language tag matches a registered handler is rendered by that
 * handler; every other block fence (e.g. ```python) renders through the
 * shared `CodeBlock` (a syntax-highlighted <pre> box with a Copy button) and
 * inline code renders as a styled inline <code>. Overriding Streamdown's
 * `code` component means this file owns block-vs-inline rendering itself:
 * Streamdown's default `pre` component is a pass-through, so a bare <code>
 * fallback would drop the <pre> wrapper and collapse the block's newlines.
 *
 * See bricks/app/overrides/MANIFEST.md "Fence-dispatch contract".
 */
import {
  type ComponentProps,
  type ReactNode,
  Suspense,
  lazy,
  memo,
  useCallback,
  useState,
} from 'react';
import { Check, Copy } from 'lucide-react';
import { DatabricksMessageCitationStreamdownIntegration } from '../databricks-message-citation';
import { Streamdown } from 'streamdown';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { cn } from '@/lib/utils';

import {
  decodeBase64Utf8,
  parseFenceHeader,
} from './message-context';
import { CardButton } from './block-card';
import { CodeBlock } from './code-block';
import { type CodeNode, fenceLanguage, isInlineCode } from './code-fence';
import { CodeBlockWithActions } from './sql-code-block';
import { ChartErrorBoundary } from './chart-error-boundary';
import { parseChartSpec } from './chart-spec';
import { ReportBlock } from './report-block';
import { REPORT_FENCE_TAG, reportFilename } from './report-fence';
import { SVG_FENCE_TAG } from './svg-fence';
import { SvgImage } from './svg-image';

const LazyPaginatedTable = lazy(() => import('./paginated-table'));
const LazyInteractiveChart = lazy(() => import('./interactive-chart'));
const LazyMermaidDiagram = lazy(() => import('./mermaid-diagram'));

export type FenceRenderer = (
  children: string,
  className: string,
) => ReactNode | null;

// Markdown images may only load from this app's own origin. Streamdown's
// default `allowedImagePrefixes` is `["*"]`, so a remote image URL the model
// emits (e.g. from injected content) would auto-load on render and beacon
// data out. Links are left at the default so source citations still work.
// The trailing slash stops look-alike hosts (`<origin>.evil.io`) from
// matching a plain string-prefix check.
const ALLOWED_IMAGE_PREFIXES =
  typeof window !== 'undefined' ? [`${window.location.origin}/`] : [];

const FENCE_RENDERERS: Record<string, FenceRenderer> = {};

export function registerFenceRenderer(lang: string, fn: FenceRenderer): void {
  FENCE_RENDERERS[lang] = fn;
}

registerFenceRenderer('sql-download', (children, className) => {
  const header = parseFenceHeader(className, 'sql-download');
  if (!header) {
    return null;
  }
  const content = children?.trim()
    ? children
    : decodeBase64Utf8(header.payload);
  return (
    <CodeBlockWithActions
      filename={header.filename}
      content={content}
      language="sql"
    />
  );
});

registerFenceRenderer('json-download', (children, className) => {
  const header = parseFenceHeader(className, 'json-download');
  if (!header) {
    return null;
  }
  const content = children?.trim()
    ? children
    : decodeBase64Utf8(header.payload);
  return (
    <CodeBlockWithActions
      filename={header.filename}
      content={content}
      language="json"
    />
  );
});

registerFenceRenderer('table-download', (_children, className) => {
  const header = parseFenceHeader(className, 'table-download');
  if (!header) {
    return null;
  }
  return (
    <Suspense fallback={null}>
      <LazyPaginatedTable
        filename={header.filename}
        payloadB64={header.payload}
      />
    </Suspense>
  );
});

registerFenceRenderer('echarts-chart', (children, className) => {
  if (typeof children !== 'string') {
    return null;
  }
  const spec = parseChartSpec(children);
  if (!spec) {
    return null;
  }
  const fallback = <code className={className}>{children}</code>;
  return (
    <ChartErrorBoundary fallback={fallback}>
      <Suspense fallback={null}>
        <LazyInteractiveChart spec={spec} />
      </Suspense>
    </ChartErrorBoundary>
  );
});

const COPY_FEEDBACK_MS = 1500;

/** Copy-to-clipboard button for the `CodeBlock` actions slot. */
function CopyButton({ code }: { code: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), COPY_FEEDBACK_MS);
    } catch {
      // Clipboard API unavailable (e.g. insecure context) — silently no-op
      // rather than throwing inside a render path.
    }
  }, [code]);
  return (
    <CardButton
      icon={copied ? Check : Copy}
      label={copied ? 'Copied' : 'Copy'}
      onClick={handleCopy}
    />
  );
}

// Streamdown passes the unist AST node through to the `code` override; its
// position lets us tell inline code from a fenced block (see `code-fence.ts`).
type CodeProps = ComponentProps<'code'> & { node?: CodeNode };

const customCodeComponent = ({
  node,
  className,
  children,
  ...rest
}: CodeProps) => {
  const { chartsEnabled, reportsEnabled } = useAppConfig();
  const lang = fenceLanguage(className);

  if (isInlineCode(node, lang)) {
    return (
      <code
        className={cn(
          'rounded bg-muted px-1.5 py-0.5 font-mono text-sm',
          className,
        )}
        {...rest}
      >
        {children}
      </code>
    );
  }

  // Registered fence handlers take precedence (contract in MANIFEST.md).
  if (lang && typeof children === 'string') {
    const handler = FENCE_RENDERERS[lang];
    // Charts and reports are gated by the spec.ui.charts / spec.ui.reports
    // toggles: when off, fall through to the highlighted code block below.
    const gated =
      (lang === 'echarts-chart' && !chartsEnabled) ||
      (lang === REPORT_FENCE_TAG && !reportsEnabled);
    if (handler && !gated) {
      const rendered = handler(children, className ?? '');
      if (rendered != null) {
        return <>{rendered}</>;
      }
    }
  }

  // Any other block fence (python, plain, or a handler that returned null):
  // render a syntax-highlighted box with a Copy button. A bare <code> would
  // lose the <pre> wrapper and collapse the block's line breaks.
  const codeText =
    typeof children === 'string'
      ? children.replace(/\n$/, '')
      : String(children ?? '');
  return <PlainCodeBlock code={codeText} language={lang ?? 'text'} />;
};

/** Syntax-highlighted block with a Copy button: the fallback for any fence. */
function PlainCodeBlock({ code, language }: { code: string; language: string }) {
  return (
    <CodeBlock code={code} language={language}>
      <CopyButton code={code} />
    </CodeBlock>
  );
}

type ResponseProps = ComponentProps<typeof Streamdown>;

export const Response = memo(
  (props: ResponseProps) => {
    return (
      <Streamdown
        allowedImagePrefixes={ALLOWED_IMAGE_PREFIXES}
        components={{
          a: DatabricksMessageCitationStreamdownIntegration,
          code: customCodeComponent,
        }}
        className="flex flex-col gap-4"
        {...props}
      />
    );
  },
  (prevProps, nextProps) => prevProps.children === nextProps.children,
);

Response.displayName = 'Response';

// Registered after `Response` because the report body renders through it.
registerFenceRenderer(REPORT_FENCE_TAG, (children, className) => {
  const markdown = children.replace(/\n$/, '');
  return (
    <ReportBlock
      filename={reportFilename(className, markdown)}
      markdown={markdown}
      actions={<CopyButton code={markdown} />}
    >
      <Response>{markdown}</Response>
    </ReportBlock>
  );
});

registerFenceRenderer('mermaid', (children) => {
  const code = children.replace(/\n$/, '');
  const fallback = <PlainCodeBlock code={code} language="mermaid" />;
  return (
    <ChartErrorBoundary fallback={fallback}>
      <Suspense fallback={fallback}>
        <LazyMermaidDiagram
          code={code}
          fallback={fallback}
          actions={<CopyButton code={code} />}
        />
      </Suspense>
    </ChartErrorBoundary>
  );
});

registerFenceRenderer(SVG_FENCE_TAG, (children) => {
  const code = children.replace(/\n$/, '');
  const fallback = <PlainCodeBlock code={code} language={SVG_FENCE_TAG} />;
  return (
    <ChartErrorBoundary fallback={fallback}>
      <SvgImage
        code={code}
        fallback={fallback}
        actions={<CopyButton code={code} />}
      />
    </ChartErrorBoundary>
  );
});
