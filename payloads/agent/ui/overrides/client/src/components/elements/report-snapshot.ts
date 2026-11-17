/**
 * Standalone HTML file of a rendered report, for the report card's HTML
 * download. The file opens in any browser and prints cleanly to PDF: it
 * carries its own stylesheet, has no scripts and needs nothing from the App.
 * Each ECharts chart becomes a PNG image (a cloned canvas loses its pixels,
 * and a fixed-width chart would not fit the page); Mermaid diagrams stay
 * inline SVG. Card toolbars (`[data-snapshot-hide]`) and buttons are left
 * out, and class attributes are dropped since the App's stylesheet is not
 * included. Classes inside an SVG stay: a Mermaid diagram's own stylesheet
 * selects its shapes and labels by class.
 */

const ECHARTS_CONTAINER = '[_echarts_instance_]';
const LEFT_OUT = '[data-snapshot-hide], button, script';

const REPORT_CSS = `
body {
  max-width: 860px;
  margin: 2rem auto;
  padding: 0 1.5rem;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6;
  color: #1f2937;
  background: #fff;
}
h1, h2, h3, h4 { line-height: 1.25; margin: 1.6em 0 0.6em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #d1d5db; padding: 0.4rem 0.6rem; text-align: left; }
th { background: #f3f4f6; }
pre { background: #f6f8fa; padding: 0.75rem 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.85em; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
img, svg { display: block; max-width: 100%; height: auto; margin: 1em auto; }
@page { margin: 18mm; }
@media print {
  body { margin: 0; max-width: none; padding: 0; }
  pre, table, img, svg { break-inside: avoid; }
  h1, h2, h3, h4 { break-after: avoid; }
}
`;

function chartImage(container: Element): HTMLImageElement {
  const image = document.createElement('img');
  image.alt = 'Chart';
  const canvas = container.querySelector('canvas');
  if (canvas) {
    image.src = canvas.toDataURL('image/png');
  }
  return image;
}

function snapshotBody(source: HTMLElement): HTMLElement {
  const images = Array.from(source.querySelectorAll(ECHARTS_CONTAINER), chartImage);
  const copy = source.cloneNode(true) as HTMLElement;
  copy.querySelectorAll(ECHARTS_CONTAINER).forEach((container, index) => {
    container.replaceWith(images[index]);
  });
  copy.querySelectorAll(LEFT_OUT).forEach((element) => element.remove());
  copy.querySelectorAll('[class]').forEach((element) => {
    if (!element.closest('svg')) {
      element.removeAttribute('class');
    }
  });
  copy.removeAttribute('class');
  return copy;
}

export function reportHtml(source: HTMLElement, title: string): string {
  const doc = document.implementation.createHTMLDocument(title);
  const charset = doc.createElement('meta');
  charset.setAttribute('charset', 'utf-8');
  const style = doc.createElement('style');
  style.textContent = REPORT_CSS;
  doc.head.prepend(charset);
  doc.head.append(style);
  doc.body.append(doc.importNode(snapshotBody(source), true));
  return `<!DOCTYPE html>\n${doc.documentElement.outerHTML}`;
}
