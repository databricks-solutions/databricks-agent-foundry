/**
 * Renderer for the ``svg`` fence handler in ``response.tsx``.
 *
 * The SVG is written by the model, so it is untrusted. It is shown as an
 * `<img>`, never inserted into the page: a browser renders an SVG image
 * without running its scripts or loading anything it links to. Text that
 * is not a well-formed SVG document (or is still streaming in) stays a
 * code block.
 */
import { type ReactNode, useCallback, useMemo } from 'react';
import { Download, Image } from 'lucide-react';

import { BlockCard, CardButton } from './block-card';
import { downloadTextFile } from './message-context';
import { SVG_CONTENT_TYPE, looksLikeSvg, svgDataUrl } from './svg-fence';

const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';
const SVG_FILENAME = 'image.svg';

interface SvgImageProps {
  code: string;
  fallback: ReactNode;
  actions?: ReactNode;
}

function isWellFormedSvg(code: string): boolean {
  if (!looksLikeSvg(code)) {
    return false;
  }
  const doc = new DOMParser().parseFromString(code, 'image/svg+xml');
  const root = doc.documentElement;
  return (
    root.localName === 'svg' &&
    root.namespaceURI === SVG_NAMESPACE &&
    doc.getElementsByTagName('parsererror').length === 0
  );
}

export function SvgImage({ code, fallback, actions }: SvgImageProps) {
  const valid = useMemo(() => isWellFormedSvg(code), [code]);

  const handleDownload = useCallback(() => {
    downloadTextFile(SVG_FILENAME, code, SVG_CONTENT_TYPE);
  }, [code]);

  if (!valid) {
    return <>{fallback}</>;
  }
  return (
    <BlockCard
      icon={Image}
      label="Image"
      actions={
        <>
          {actions}
          <CardButton
            icon={Download}
            label="SVG"
            ariaLabel="Download image as SVG"
            onClick={handleDownload}
          />
        </>
      }
    >
      <img
        src={svgDataUrl(code)}
        alt="SVG image"
        className="mx-auto h-auto max-w-full"
      />
    </BlockCard>
  );
}

export default SvgImage;
