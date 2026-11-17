/**
 * Mermaid renderer for the ``mermaid`` fence handler in ``response.tsx``.
 *
 * Streamdown renders Mermaid only inside its own ``code`` component, which
 * ``response.tsx`` replaces, and it does not export that renderer. This
 * module is ``lazy()``-loaded so Mermaid only ships when a diagram renders.
 * Settings follow Streamdown's: ``securityLevel: 'strict'`` (no scripts or
 * click handlers in diagrams) and no error SVG injected into the page.
 *
 * While a diagram streams in, its text is often not valid yet: the last
 * valid diagram stays on screen, and the code block shows until there is one.
 */
import { type ReactNode, useCallback, useEffect, useState } from 'react';
import { Download, Workflow } from 'lucide-react';
import mermaid from 'mermaid';
import { useTheme } from 'next-themes';

import { BlockCard, CardButton } from './block-card';
import { downloadTextFile } from './message-context';
import { SVG_CONTENT_TYPE } from './svg-fence';

const SVG_FILENAME = 'diagram.svg';

interface MermaidDiagramProps {
  code: string;
  fallback: ReactNode;
  actions?: ReactNode;
}

let renderCount = 0;

async function renderSvg(code: string, dark: boolean): Promise<string | null> {
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    suppressErrorRendering: true,
    theme: dark ? 'dark' : 'default',
  });
  if (!(await mermaid.parse(code, { suppressErrors: true }))) {
    return null;
  }
  renderCount += 1;
  const { svg } = await mermaid.render(`mermaid-diagram-${renderCount}`, code);
  return svg;
}

/** Mermaid returns HTML-serialized SVG (e.g. `<br>` in labels); a standalone .svg file must be well-formed XML. */
function svgAsXml(svg: string): string {
  const element = new DOMParser()
    .parseFromString(svg, 'text/html')
    .querySelector('svg');
  return element ? new XMLSerializer().serializeToString(element) : svg;
}

export function MermaidDiagram({
  code,
  fallback,
  actions,
}: MermaidDiagramProps) {
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === 'dark';
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    renderSvg(code, dark)
      .then((next) => {
        if (current && next) {
          setSvg(next);
        }
      })
      .catch(() => {
        // Keep the last valid diagram (or the fallback) on a render error.
      });
    return () => {
      current = false;
    };
  }, [code, dark]);

  const handleDownload = useCallback(() => {
    if (svg) {
      downloadTextFile(SVG_FILENAME, svgAsXml(svg), SVG_CONTENT_TYPE);
    }
  }, [svg]);

  if (!svg) {
    return <>{fallback}</>;
  }
  return (
    <BlockCard
      icon={Workflow}
      label="Diagram"
      actions={
        <>
          {actions}
          <CardButton
            icon={Download}
            label="SVG"
            ariaLabel="Download diagram as SVG"
            onClick={handleDownload}
          />
        </>
      }
    >
      <div
        role="img"
        aria-label="Mermaid diagram"
        className="flex justify-center overflow-x-auto"
        // Mermaid's strict security level sanitizes the SVG it returns.
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </BlockCard>
  );
}

export default MermaidDiagram;
