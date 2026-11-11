/**
 * Override of e2e-chatbot-app-next/client/src/components/elements/response.tsx.
 *
 * Adds a fence-dispatch hook on top of the upstream Streamdown wrapper:
 * features (#122 SQL rendering, #105 chart rendering) register handlers
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
import { CodeBlock } from './code-block';
import { type CodeNode, fenceLanguage, isInlineCode } from './code-fence';
import { CodeBlockWithActions } from './sql-code-block';
import { ChartErrorBoundary } from './chart-error-boundary';
import { parseChartSpec } from './chart-spec';

const LazyPaginatedTable = lazy(() => import('./paginated-table'));
const LazyInteractiveChart = lazy(() => import('./interactive-chart'));

export type FenceRenderer = (
  children: string,
  className: string,
) => ReactNode | null;

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
    <button
      type="button"
      onClick={handleCopy}
      className="inline-flex items-center gap-1 rounded px-2 py-1 text-muted-foreground text-xs hover:bg-muted"
      aria-label={copied ? 'Copied' : 'Copy'}
    >
      {copied ? (
        <Check className="h-3.5 w-3.5" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
      {copied ? 'Copied' : 'Copy'}
    </button>
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
  const { chartsEnabled } = useAppConfig();
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
    // Chart rendering is gated by the spec.ui.charts toggle: when off,
    // fall through to the highlighted code block below.
    const gated = lang === 'echarts-chart' && !chartsEnabled;
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
  return (
    <CodeBlock code={codeText} language={lang ?? 'text'}>
      <CopyButton code={codeText} />
    </CodeBlock>
  );
};

type ResponseProps = ComponentProps<typeof Streamdown>;

export const Response = memo(
  (props: ResponseProps) => {
    return (
      <Streamdown
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
